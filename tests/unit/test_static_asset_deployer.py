"""Exact-version static-site custom-resource behavior."""

from __future__ import annotations

import hashlib
import io
import stat
import zipfile

import pytest

from src.gateway.deployment.infra.static_asset_deployer import (
    deploy,
    remove,
)


def _archive(
    entries: dict[str, bytes],
    *,
    symlink: str | None = None,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
    ) as archive:
        for name, payload in sorted(entries.items()):
            information = zipfile.ZipInfo(
                name,
                date_time=(1980, 1, 1, 0, 0, 0),
            )
            information.compress_type = zipfile.ZIP_DEFLATED
            information.create_system = 3
            mode = stat.S_IFLNK | 0o777 if name == symlink else stat.S_IFREG | 0o644
            information.external_attr = mode << 16
            archive.writestr(information, payload)
    return output.getvalue()


class _S3:
    def __init__(
        self,
        source: bytes,
        *,
        destination: dict[str, bytes] | None = None,
    ) -> None:
        self.source = source
        self.destination = dict(destination or {})
        self.get_requests: list[dict] = []
        self.put_requests: list[dict] = []
        self.delete_requests: list[dict] = []

    def get_object(self, **request):
        self.get_requests.append(request)
        return {"Body": io.BytesIO(self.source)}

    def list_objects_v2(self, **request):
        return {
            "Contents": [{"Key": key} for key in sorted(self.destination)],
            "IsTruncated": False,
        }

    def put_object(self, **request):
        self.put_requests.append(request)
        self.destination[request["Key"]] = request["Body"]
        return {}

    def delete_objects(self, **request):
        self.delete_requests.append(request)
        for item in request["Delete"]["Objects"]:
            self.destination.pop(item["Key"], None)
        return {}


class _CloudFront:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def create_invalidation(self, **request):
        self.requests.append(request)
        return {}


def _properties(source: bytes, *, retain: str = "false") -> dict:
    return {
        "DestinationBucket": "site-bucket",
        "DistributionId": "EDISTRIBUTION",
        "RetainOnDelete": retain,
        "SourceBucket": "artifact-bucket",
        "SourceKey": "release/static.zip",
        "SourceSha256": hashlib.sha256(source).hexdigest(),
        "SourceVersion": "immutable-version",
    }


def test_deploy_verifies_version_uploads_prunes_and_invalidates() -> None:
    source = _archive(
        {
            "admin/static/app.js": b"console.log('ok');\n",
            "index.html": b"<h1>AxonLLM</h1>\n",
        }
    )
    s3 = _S3(source, destination={"old.txt": b"old"})
    cloudfront = _CloudFront()

    result = deploy(
        _properties(source),
        request_id="request-1",
        s3_client=s3,
        cloudfront_client=cloudfront,
    )

    assert s3.get_requests == [
        {
            "Bucket": "artifact-bucket",
            "Key": "release/static.zip",
            "VersionId": "immutable-version",
        }
    ]
    assert s3.destination == {
        "admin/static/app.js": b"console.log('ok');\n",
        "index.html": b"<h1>AxonLLM</h1>\n",
    }
    by_key = {request["Key"]: request for request in s3.put_requests}
    assert by_key["index.html"]["CacheControl"] == "no-cache"
    assert by_key["admin/static/app.js"]["CacheControl"] == "public,max-age=3600"
    assert s3.delete_requests[0]["Delete"]["Objects"] == [{"Key": "old.txt"}]
    assert cloudfront.requests == [
        {
            "DistributionId": "EDISTRIBUTION",
            "InvalidationBatch": {
                "CallerReference": "request-1",
                "Paths": {"Items": ["/*"], "Quantity": 1},
            },
        }
    ]
    assert result == {
        "ObjectCount": 2,
        "SourceSha256": hashlib.sha256(source).hexdigest(),
        "SourceVersion": "immutable-version",
    }


def test_deploy_invalidates_qualification_and_production_distributions() -> None:
    source = _archive({"index.html": b"<h1>AxonLLM</h1>\n"})
    s3 = _S3(source)
    cloudfront = _CloudFront()
    properties = _properties(source)
    properties["AdditionalDistributionId"] = "EPRODUCTION"

    deploy(
        properties,
        request_id="request-edge",
        s3_client=s3,
        cloudfront_client=cloudfront,
    )

    assert [request["DistributionId"] for request in cloudfront.requests] == ["EDISTRIBUTION", "EPRODUCTION"]
    assert [request["InvalidationBatch"]["CallerReference"] for request in cloudfront.requests] == [
        "request-edge",
        "request-edge",
    ]


def test_additional_distribution_id_must_be_a_string() -> None:
    source = _archive({"index.html": b"safe\n"})
    properties = _properties(source)
    properties["AdditionalDistributionId"] = ["EPRODUCTION"]

    with pytest.raises(
        ValueError,
        match="AdditionalDistributionId must be a string",
    ):
        deploy(
            properties,
            request_id="request-invalid-edge",
            s3_client=_S3(source),
            cloudfront_client=_CloudFront(),
        )


def test_digest_mismatch_fails_before_upload() -> None:
    source = _archive({"index.html": b"safe\n"})
    s3 = _S3(source)
    cloudfront = _CloudFront()
    properties = _properties(source)
    properties["SourceSha256"] = "0" * 64

    with pytest.raises(ValueError, match="digest does not match"):
        deploy(
            properties,
            request_id="request-2",
            s3_client=s3,
            cloudfront_client=cloudfront,
        )

    assert s3.put_requests == []
    assert cloudfront.requests == []


def test_symlink_entry_is_rejected() -> None:
    source = _archive(
        {"index.html": b"target", "linked.html": b"index.html"},
        symlink="linked.html",
    )
    s3 = _S3(source)

    with pytest.raises(ValueError, match="non-regular"):
        deploy(
            _properties(source),
            request_id="request-3",
            s3_client=s3,
            cloudfront_client=_CloudFront(),
        )

    assert s3.put_requests == []


def test_production_delete_retains_objects_without_invalidation() -> None:
    s3 = _S3(b"", destination={"index.html": b"retained"})
    cloudfront = _CloudFront()

    result = remove(
        _properties(b"", retain="true"),
        request_id="request-4",
        s3_client=s3,
        cloudfront_client=cloudfront,
    )

    assert result == {"Retained": True}
    assert s3.destination == {"index.html": b"retained"}
    assert s3.delete_requests == []
    assert cloudfront.requests == []


def test_namespaced_delete_removes_objects_and_invalidates() -> None:
    s3 = _S3(b"", destination={"a": b"1", "b": b"2"})
    cloudfront = _CloudFront()

    result = remove(
        _properties(b""),
        request_id="request-5",
        s3_client=s3,
        cloudfront_client=cloudfront,
    )

    assert result == {"DeletedObjectCount": 2, "Retained": False}
    assert s3.destination == {}
    assert cloudfront.requests[0]["DistributionId"] == "EDISTRIBUTION"
