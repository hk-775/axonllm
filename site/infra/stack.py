"""S3 + CloudFront stack for the AxonLLM landing page.

The bucket is private and reachable only through the distribution's origin
access identity — a public bucket would serve the same bytes over plain HTTP
from a second, unversioned URL that bypasses the cache invalidation below.
"""

from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_route53 as route53
from aws_cdk import aws_route53_targets as targets
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct


class AxonLLMSiteStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        domain_name: str | None = None,
        hosted_zone_id: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            # A static marketing page holds no state worth preserving; leaving
            # the bucket behind on destroy means the next deploy of the same
            # stack name collides with it.
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        oai = cloudfront.OriginAccessIdentity(
            self, "SiteOAI", comment="AxonLLM landing page"
        )
        bucket.grant_read(oai)

        certificate = None
        hosted_zone = None
        # Both are required together: a cert with no Route53 record is issued
        # against a domain nothing points at, and validation never completes.
        if domain_name and hosted_zone_id:
            hosted_zone = route53.HostedZone.from_hosted_zone_attributes(
                self,
                "HostedZone",
                hosted_zone_id=hosted_zone_id,
                zone_name=domain_name,
            )
            certificate = acm.Certificate(
                self,
                "SiteCertificate",
                domain_name=domain_name,
                subject_alternative_names=[f"www.{domain_name}"],
                validation=acm.CertificateValidation.from_dns(hosted_zone),
            )

        distribution = cloudfront.Distribution(
            self,
            "SiteDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3Origin(bucket, origin_access_identity=oai),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                compress=True,
            ),
            default_root_object="index.html",
            domain_names=[domain_name, f"www.{domain_name}"] if certificate else None,
            certificate=certificate,
            error_responses=[
                # Serve index.html for unknown paths so a mistyped or shared
                # deep link lands on the page instead of CloudFront's XML.
                # ttl=0 keeps a genuine 404 from being cached as a 200.
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=None,
                ),
            ],
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
        )

        s3deploy.BucketDeployment(
            self,
            "DeploySite",
            sources=[s3deploy.Source.asset("..", exclude=["infra", "infra/**"])],
            destination_bucket=bucket,
            distribution=distribution,
            # Without an invalidation the old page keeps being served from
            # every edge until the cache expires, so a deploy looks like a
            # no-op for hours.
            distribution_paths=["/*"],
        )

        if hosted_zone and domain_name:
            route53.ARecord(
                self,
                "SiteAliasRecord",
                zone=hosted_zone,
                record_name=domain_name,
                target=route53.RecordTarget.from_alias(
                    targets.CloudFrontTarget(distribution)
                ),
            )
            route53.ARecord(
                self,
                "SiteWwwAliasRecord",
                zone=hosted_zone,
                record_name=f"www.{domain_name}",
                target=route53.RecordTarget.from_alias(
                    targets.CloudFrontTarget(distribution)
                ),
            )

        CfnOutput(
            self,
            "SiteURL",
            value=f"https://{domain_name}" if domain_name else f"https://{distribution.domain_name}",
            description="Landing page URL",
        )
        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "DistributionId", value=distribution.distribution_id)
