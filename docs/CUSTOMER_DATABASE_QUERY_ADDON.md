# Customer Database Query Add-On

Customer database querying is not part of the default AxonLLM deployment.
AgentCore and standalone deployments ship the same core product surface without
Athena, customer datasource roles, or query-specific STS permissions.

## Core Boundary

A default installation:

- does not register `POST /v1/query` or an AgentCore `query` action;
- does not expose `/admin/datasources`;
- does not install `sqlglot`;
- does not create Athena or STS VPC endpoints;
- does not grant Athena actions or permission to assume customer datasource
  roles;
- does not require a datasource, workgroup, result bucket, or query canary for
  deployment certification.

Legacy cleanup fields and deployment fingerprints may remain readable during
an upgrade so an older installation can be removed safely. They do not enable
query execution.

## Add-On Contract

The repository retains the query engine and datasource implementation as the
starting point for an explicit add-on. Its parser dependency is available
through:

```bash
pip install 'axon-llm[customer-database-query]'
```

Installing the dependency alone does not activate the feature. A supported
add-on release must separately provide:

1. HTTP and AgentCore route registration for the same query service.
2. Tenant-scoped datasource administration.
3. Exact customer role, Athena workgroup, catalog, database, result bucket, and
   KMS bindings.
4. Least-privilege Athena and STS networking and IAM.
5. Query-specific audit, admission, interruption recovery, and certification.
6. The same API and administrative experience on standalone and AgentCore.

The customer owns the queried database and grants only read access to the
specific data required. AxonLLM must use temporary credentials and must not
store customer database credentials.

## Deployment Rule

The add-on is independent of execution target. Enabling it for a release must
enable and certify it on both standalone and AgentCore; deployment target alone
must never change the product feature set.

Until that add-on packaging and deployment contract is released, customer
database querying is unsupported in the core installer.
