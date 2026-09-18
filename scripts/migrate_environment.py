#!/usr/bin/env python3
"""Safely migrate GitHub environment variables and environment secrets.

Environment variables can be read and written through the GitHub REST API.

Environment secret values cannot be read through the GitHub REST API. For a
secret migration, the workflow must attach the job to the source environment
and pass one source secret value through SOURCE_SECRET_VALUE. This script then
encrypts that value with the destination environment public key and writes it
back through the API.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable


API_ROOT = "https://api.github.com"
API_VERSION = "2026-03-10"


class MigrationError(RuntimeError):
    pass


@dataclass
class GitHubApi:
    token: str

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        expected: Iterable[int] = (200,),
    ) -> Any:
        url = f"{API_ROOT}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "environment-migration-automation",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                if response.status not in expected:
                    raise MigrationError(
                        f"Unexpected GitHub API status {response.status} for {method} {path}"
                    )
                if not raw:
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                message = json.loads(body).get("message", body)
            except json.JSONDecodeError:
                message = body
            raise MigrationError(
                f"GitHub API {method} {path} failed with HTTP {exc.code}: {message}"
            ) from exc
        except urllib.error.URLError as exc:
            raise MigrationError(f"Unable to reach GitHub API: {exc.reason}") from exc


def required_token() -> str:
    token = os.getenv("GH_TOKEN", "").strip()
    if not token:
        raise MigrationError(
            "GH_TOKEN is not set. Configure repository secret ENV_MIGRATION_TOKEN."
        )
    return token


def quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def repo_path(org: str, repository: str) -> str:
    return f"/repos/{quote(org)}/{quote(repository)}"


def environment_path(org: str, repository: str, environment: str) -> str:
    return f"{repo_path(org, repository)}/environments/{quote(environment)}"


def assert_environment_exists(
    api: GitHubApi, org: str, repository: str, environment: str
) -> None:
    api.request("GET", environment_path(org, repository, environment), expected=(200,))


def paginated_items(
    api: GitHubApi,
    path: str,
    collection_key: str,
    *,
    per_page: int = 30,
) -> list[dict[str, Any]]:
    page = 1
    items: list[dict[str, Any]] = []
    while True:
        separator = "&" if "?" in path else "?"
        payload = api.request(
            "GET",
            f"{path}{separator}per_page={per_page}&page={page}",
            expected=(200,),
        )
        batch = payload.get(collection_key, [])
        items.extend(batch)
        if len(batch) < per_page:
            break
        page += 1
    return items


def list_environment_variables(
    api: GitHubApi, org: str, repository: str, environment: str
) -> list[dict[str, Any]]:
    path = f"{environment_path(org, repository, environment)}/variables"
    return paginated_items(api, path, "variables", per_page=30)


def list_environment_secret_names(
    api: GitHubApi, org: str, repository: str, environment: str
) -> list[str]:
    path = f"{environment_path(org, repository, environment)}/secrets"
    secrets = paginated_items(api, path, "secrets", per_page=100)
    return sorted(item["name"] for item in secrets)


def create_environment_variable(
    api: GitHubApi,
    org: str,
    repository: str,
    environment: str,
    name: str,
    value: str,
) -> None:
    api.request(
        "POST",
        f"{environment_path(org, repository, environment)}/variables",
        {"name": name, "value": value},
        expected=(201,),
    )


def update_environment_variable(
    api: GitHubApi,
    org: str,
    repository: str,
    environment: str,
    name: str,
    value: str,
) -> None:
    api.request(
        "PATCH",
        f"{environment_path(org, repository, environment)}/variables/{quote(name)}",
        {"name": name, "value": value},
        expected=(204,),
    )


def get_environment_secret_public_key(
    api: GitHubApi, org: str, repository: str, environment: str
) -> dict[str, str]:
    return api.request(
        "GET",
        f"{environment_path(org, repository, environment)}/secrets/public-key",
        expected=(200,),
    )


def put_environment_secret(
    api: GitHubApi,
    org: str,
    repository: str,
    environment: str,
    name: str,
    encrypted_value: str,
    key_id: str,
) -> None:
    api.request(
        "PUT",
        f"{environment_path(org, repository, environment)}/secrets/{quote(name)}",
        {"encrypted_value": encrypted_value, "key_id": key_id},
        expected=(201, 204),
    )


def encrypt_secret(public_key_b64: str, plaintext: str) -> str:
    try:
        from nacl import encoding, public
    except ImportError as exc:
        raise MigrationError(
            "PyNaCl is required for non-dry-run secret migration."
        ) from exc

    public_key = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder())
    sealed_box = public.SealedBox(public_key)
    encrypted = sealed_box.encrypt(plaintext.encode("utf-8"))
    return base64.b64encode(encrypted).decode("ascii")


def write_output(path: str | None, key: str, value: str) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(f"{key}={value}\n")


def command_validate(args: argparse.Namespace) -> None:
    if args.from_environment.casefold() == args.to_environment.casefold():
        raise MigrationError("Source and target environments must be different.")

    api = GitHubApi(required_token())
    assert_environment_exists(api, args.organization, args.repository, args.from_environment)
    assert_environment_exists(api, args.organization, args.repository, args.to_environment)

    migrate_vars = args.migration_type in {"vars", "both"}
    migrate_secrets = args.migration_type in {"secrets", "both"}
    requested_repo = f"{args.organization}/{args.repository}"

    if migrate_secrets and requested_repo.casefold() != args.current_repository.casefold():
        raise MigrationError(
            "Secret migration cannot be performed cross-repository from a central "
            "workflow because GitHub never exposes environment secret values via "
            "the REST API. Run this workflow from the target repository (or call a "
            "reusable workflow from that repository) so the job can attach to the "
            "source environment."
        )

    write_output(args.github_output, "repo_full_name", requested_repo)
    write_output(args.github_output, "migrate_vars", str(migrate_vars).lower())
    write_output(args.github_output, "migrate_secrets", str(migrate_secrets).lower())

    print(
        f"Validated {requested_repo}: "
        f"{args.from_environment} -> {args.to_environment}; "
        f"type={args.migration_type}"
    )


def command_copy_vars(args: argparse.Namespace) -> None:
    api = GitHubApi(required_token())
    source = list_environment_variables(
        api, args.organization, args.repository, args.from_environment
    )
    target = list_environment_variables(
        api, args.organization, args.repository, args.to_environment
    )
    target_names = {item["name"].casefold(): item["name"] for item in target}

    created = 0
    updated = 0
    skipped = 0

    if not source:
        print("Source environment has no variables.")
        return

    for item in source:
        name = item["name"]
        value = item["value"]
        existing_name = target_names.get(name.casefold())

        if existing_name:
            if not args.overwrite:
                print(f"SKIP variable {name}: already exists in target.")
                skipped += 1
                continue

            if args.dry_run:
                print(f"DRY-RUN update variable {name}.")
            else:
                update_environment_variable(
                    api,
                    args.organization,
                    args.repository,
                    args.to_environment,
                    existing_name,
                    value,
                )
                print(f"UPDATED variable {name}.")
            updated += 1
        else:
            if args.dry_run:
                print(f"DRY-RUN create variable {name}.")
            else:
                create_environment_variable(
                    api,
                    args.organization,
                    args.repository,
                    args.to_environment,
                    name,
                    value,
                )
                print(f"CREATED variable {name}.")
            created += 1

    print(
        f"Variable migration summary: created={created}, updated={updated}, "
        f"skipped={skipped}, dry_run={args.dry_run}"
    )


def command_list_secret_names(args: argparse.Namespace) -> None:
    api = GitHubApi(required_token())
    names = list_environment_secret_names(
        api, args.organization, args.repository, args.environment
    )
    print(json.dumps(names, separators=(",", ":")))


def command_copy_secret(args: argparse.Namespace) -> None:
    api = GitHubApi(required_token())

    target_names = {
        name.casefold(): name
        for name in list_environment_secret_names(
            api, args.organization, args.repository, args.to_environment
        )
    }

    if args.secret_name.casefold() in target_names and not args.overwrite:
        print(f"SKIP secret {args.secret_name}: already exists in target.")
        return

    action = (
        "update"
        if args.secret_name.casefold() in target_names
        else "create"
    )

    if args.dry_run:
        print(f"DRY-RUN {action} secret {args.secret_name}.")
        return

    if "SOURCE_SECRET_VALUE" not in os.environ:
        raise MigrationError(
            "SOURCE_SECRET_VALUE is unavailable. The job must be attached to the "
            "source GitHub environment."
        )

    plaintext = os.environ["SOURCE_SECRET_VALUE"]
    key = get_environment_secret_public_key(
        api, args.organization, args.repository, args.to_environment
    )
    encrypted_value = encrypt_secret(key["key"], plaintext)

    put_environment_secret(
        api,
        args.organization,
        args.repository,
        args.to_environment,
        args.secret_name,
        encrypted_value,
        key["key_id"],
    )
    print(f"{action.upper()}D secret {args.secret_name}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--organization", required=True)
    validate.add_argument("--repository", required=True)
    validate.add_argument("--from-environment", required=True)
    validate.add_argument("--to-environment", required=True)
    validate.add_argument(
        "--migration-type", choices=("vars", "secrets", "both"), required=True
    )
    validate.add_argument("--current-repository", required=True)
    validate.add_argument("--github-output")
    validate.set_defaults(func=command_validate)

    copy_vars = subparsers.add_parser("copy-vars")
    copy_vars.add_argument("--organization", required=True)
    copy_vars.add_argument("--repository", required=True)
    copy_vars.add_argument("--from-environment", required=True)
    copy_vars.add_argument("--to-environment", required=True)
    copy_vars.add_argument("--overwrite", action="store_true")
    copy_vars.add_argument("--dry-run", action="store_true")
    copy_vars.set_defaults(func=command_copy_vars)

    list_secrets = subparsers.add_parser("list-secret-names")
    list_secrets.add_argument("--organization", required=True)
    list_secrets.add_argument("--repository", required=True)
    list_secrets.add_argument("--environment", required=True)
    list_secrets.set_defaults(func=command_list_secret_names)

    copy_secret = subparsers.add_parser("copy-secret")
    copy_secret.add_argument("--organization", required=True)
    copy_secret.add_argument("--repository", required=True)
    copy_secret.add_argument("--to-environment", required=True)
    copy_secret.add_argument("--secret-name", required=True)
    copy_secret.add_argument("--overwrite", action="store_true")
    copy_secret.add_argument("--dry-run", action="store_true")
    copy_secret.set_defaults(func=command_copy_secret)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except MigrationError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
