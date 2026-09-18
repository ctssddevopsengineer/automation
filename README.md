# GitHub Environment Migration Automation

This repository provides a manually triggered GitHub Actions workflow for
copying GitHub **environment variables** and, where GitHub's security model
allows it, **environment secrets** from one environment to another.

## Workflow inputs

| Input | Description |
|---|---|
| `organization` | GitHub organization / repository owner |
| `repository_name` | Repository containing the environments |
| `from_environment` | Source environment |
| `to_environment` | Destination environment |
| `migration_type` | `vars`, `secrets`, or `both` |
| `overwrite` | Replace names that already exist in the destination |
| `dry_run` | Preview changes without modifying GitHub |

Safe defaults are used:

```text
overwrite = false
dry_run   = true
```

## Authentication

Create a repository secret in this automation repository named:

```text
ENV_MIGRATION_TOKEN
```

Use a fine-grained PAT or GitHub App token with only the permissions required
for the repositories you intend to manage.

For environment variables, the credential needs permission to read and write
repository environments / environment variables.

For environment secrets, it must be able to list and create/update environment
secrets. GitHub never returns the plaintext values of stored secrets through
the REST API.

## Secret migration limitation

A central workflow can copy variables for another repository because variable
values are readable through the GitHub API.

Secrets are different: the GitHub API returns only their names and metadata,
never their plaintext values. Therefore a workflow running in this central
`automation` repository cannot pull secret values out of an arbitrary target
repository.

For `secrets` or `both`, this workflow intentionally requires:

```text
organization/repository_name == github.repository
```

The secret-copy job then attaches to the source GitHub Environment. GitHub
releases that environment's secrets to the runner only after any environment
protection rules have passed.

For organization-wide secret migration, use one of these patterns:

1. Keep a lightweight dispatcher workflow in each target repository.
2. Call centrally maintained reusable logic from the target repository.
3. Keep the source of truth in a secret manager (Vault, Azure Key Vault,
   AWS Secrets Manager, etc.) and publish the same value to each environment.

## Variable migration

The Python utility:

1. validates source and target environments,
2. lists all source environment variables,
3. lists existing target environment variables,
4. creates missing variables,
5. updates existing variables only when `overwrite=true`,
6. supports dry-run mode.

Variable values are never printed to the logs.

## Secret migration

The workflow:

1. lists only source secret names,
2. creates one job per secret,
3. binds each job to the source environment,
4. reads that single secret through `secrets[...]`,
5. fetches the destination environment public key,
6. encrypts the value with LibSodium / PyNaCl,
7. creates or updates the destination secret.

The plaintext secret is never printed.

## Recommended first execution

Start with:

```text
migration_type = vars
overwrite      = false
dry_run        = true
```

Review the Actions log, then rerun with `dry_run=false` when the planned
changes are correct.
