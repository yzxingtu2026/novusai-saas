"""中文: 主机交付证据的严格合同，不执行网络、Docker、启动或钩子。
EN: Typed host delivery evidence; no network, Docker, startup or hooks.

中文: 输入来自可信主机观察器的明确私密文件；校验JSON不能替代实际主机观测。
The trust boundary is an explicitly supplied private observation file produced by
the trusted host observer. These validators compare its measurements with frozen
release bytes; they cannot turn operator-authored JSON into an actual host probe.
No HTTP writer is provided. The real publisher must run that observer separately.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sha = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Digest = Annotated[str, Field(pattern=r"^sha256:[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Positive = Annotated[int, Field(gt=0)]
Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=100)]
Role = Literal["backend", "worker", "beat"]
ROLES = {"backend", "worker", "beat"}


def canonical_sha(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


def timestamp(value: str) -> datetime:
    # 中文: Docker RFC3339使用Z和纳秒；Python3.10只接受规范偏移/微秒。
    # EN: Normalize Docker's UTC Z/nanoseconds for Python 3.10's ISO parser.
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    fraction = re.fullmatch(
        r"(.+T\d{2}:\d{2}:\d{2})\.(\d+)([+-]\d{2}:\d{2})", normalized
    )
    if fraction:
        normalized = fraction[1] + "." + fraction[2][:6].ljust(6, "0") + fraction[3]
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("plugin_image_delivery_time_invalid")
    return parsed.astimezone(timezone.utc)


class StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    def model_dump(self, **kwargs):
        kwargs.setdefault("by_alias", True)
        return super().model_dump(**kwargs)


class TreeEntry(StrictContract):
    path: Annotated[str, Field(min_length=1, max_length=512)]
    kind: Literal["file", "directory"]
    size: Annotated[int, Field(ge=0)]
    sha256: Sha | None

    @model_validator(mode="after")
    def entry(self):
        if (
            not self.path
            or any(char in self.path for char in "\\:\x00")
            or any(part in {"", ".", ".."} for part in self.path.split("/"))
        ):
            raise ValueError("plugin_image_delivery_entry_invalid")
        if self.kind == "directory" and (self.size != 0 or self.sha256 is not None):
            raise ValueError("plugin_image_delivery_entry_invalid")
        if self.kind == "file" and self.sha256 is None:
            raise ValueError("plugin_image_delivery_entry_invalid")
        return self


def tree_sha(entries: list[TreeEntry]) -> str:
    names = {entry.path: entry for entry in entries}
    if len(names) != len(entries):
        raise ValueError("plugin_image_delivery_tree_duplicate")
    for name in names:
        parent = name.rpartition("/")[0]
        if parent and (parent not in names or names[parent].kind != "directory"):
            raise ValueError("plugin_image_delivery_tree_parent")
    rows = [
        (entry.path, entry.kind)
        if entry.kind == "directory"
        else (entry.path, entry.kind, entry.size, entry.sha256)
        for entry in sorted(entries, key=lambda entry: entry.path.split("/"))
    ]
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class BuildContract(StrictContract):
    schema_version: Literal["novusai-plugin-image-build/v1"] = Field(alias="schema")
    request_id: Identifier
    operation: Literal["install", "upgrade", "rollback"]
    operator_id: Positive
    plugin_name: Name
    source_plugin_id: Positive | None
    target_plugin_version_id: Positive | None
    source_version: Annotated[str, Field(min_length=1, max_length=50)]
    target_version: Annotated[str, Field(min_length=1, max_length=50)]
    source_status: Literal["enabled", "disabled", "installed", "not_installed"]
    source_manifest_sha256: Sha
    target_manifest_sha256: Sha
    archive_sha256: Sha
    source_archive_sha256: Sha | None
    source_tree_sha256: Sha
    target_tree_sha256: Sha
    archive_members: Annotated[list[TreeEntry], Field(min_length=1, max_length=2000)]
    source_archive_members: Annotated[list[TreeEntry], Field(max_length=2000)]

    @model_validator(mode="after")
    def trees(self):
        if tree_sha(self.archive_members) != self.target_tree_sha256 or not any(
            entry.path == "plugin.yaml" and entry.kind == "file"
            for entry in self.archive_members
        ):
            raise ValueError("plugin_image_delivery_target_tree")
        if self.operation == "install":
            if (
                self.source_plugin_id is not None
                or self.source_archive_sha256 is not None
                or self.source_archive_members
                or self.source_tree_sha256 != "0" * 64
                or self.source_status != "not_installed"
                or self.source_version != "__uninstalled__"
                or self.source_manifest_sha256 != canonical_sha({})
            ):
                raise ValueError("plugin_image_delivery_install_identity")
        elif (
            self.source_plugin_id is None
            or self.source_archive_sha256 is None
            or tree_sha(self.source_archive_members) != self.source_tree_sha256
            or self.source_status == "not_installed"
            or self.source_version == self.target_version
        ):
            raise ValueError("plugin_image_delivery_source_tree")
        if (self.operation == "rollback") != (
            self.target_plugin_version_id is not None
        ):
            raise ValueError("plugin_image_delivery_history_identity")
        return self


class ImageIdentity(StrictContract):
    reference: Annotated[
        str, Field(pattern=r"^[a-z0-9][a-z0-9.:/-]+@sha256:[a-f0-9]{64}$")
    ]
    daemon_image_id: Digest
    manifest_base64: Annotated[str, Field(max_length=1_333_336)]
    config_base64: Annotated[str, Field(max_length=1_333_336)]

    @model_validator(mode="after")
    def oci(self):
        manifest_raw = base64.b64decode(self.manifest_base64, validate=True)
        config_raw = base64.b64decode(self.config_base64, validate=True)
        if (
            not 0 < len(manifest_raw) <= 1_000_000
            or not 0 < len(config_raw) <= 1_000_000
        ):
            raise ValueError("plugin_image_delivery_oci_size")
        manifest, config = strict_json(manifest_raw), strict_json(config_raw)
        manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
        config_digest = "sha256:" + hashlib.sha256(config_raw).hexdigest()
        descriptor = manifest["config"]
        if self.reference.rsplit("@", 1)[
            1
        ] != manifest_digest or self.daemon_image_id not in {
            manifest_digest,
            config_digest,
        }:
            raise ValueError("plugin_image_delivery_image_digest")
        if (
            manifest.get("schemaVersion") != 2
            or type(manifest.get("schemaVersion")) is not int
            or manifest.get("mediaType")
            not in {
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            }
        ):
            raise ValueError("plugin_image_delivery_manifest_type")
        if (
            descriptor.get("digest") != config_digest
            or descriptor.get("size") != len(config_raw)
            or type(descriptor.get("size")) is not int
            or descriptor.get("mediaType")
            not in {
                "application/vnd.oci.image.config.v1+json",
                "application/vnd.docker.container.image.v1+json",
            }
        ):
            raise ValueError("plugin_image_delivery_config_digest")
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ValueError("plugin_image_delivery_platform")
        layers, rootfs = manifest.get("layers"), config.get("rootfs")
        if (
            type(layers) is not list
            or not layers
            or type(rootfs) is not dict
            or rootfs.get("type") != "layers"
            or type(rootfs.get("diff_ids")) is not list
            or len(rootfs["diff_ids"]) != len(layers)
        ):
            raise ValueError("plugin_image_delivery_rootfs")
        for layer, diff_id in zip(layers, rootfs["diff_ids"], strict=True):
            from pydantic import TypeAdapter

            TypeAdapter(Digest).validate_python(diff_id, strict=True)
            TypeAdapter(Digest).validate_python(layer["digest"], strict=True)
            if (
                type(layer.get("size")) is not int
                or layer["size"] <= 0
                or layer.get("mediaType")
                not in {
                    "application/vnd.oci.image.layer.v1.tar+gzip",
                    "application/vnd.oci.image.layer.v1.tar",
                    "application/vnd.oci.image.layer.v1.tar+zstd",
                    "application/vnd.docker.image.rootfs.diff.tar.gzip",
                }
            ):
                raise ValueError("plugin_image_delivery_layer")
        return self


class ProbeContract(StrictContract):
    health_source: Literal["docker_healthcheck", "readiness_probe"]
    sha256: Sha


class ReleaseContract(StrictContract):
    schema_version: Literal["novusai-design-plugin-image-release/v1"] = Field(
        alias="schema"
    )
    operation_id: Identifier
    build_contract: BuildContract
    required_roles: list[Role]
    images: dict[Role, ImageIdentity]
    observer_sha256: Sha
    role_probes: dict[Role, ProbeContract]

    @model_validator(mode="after")
    def roles(self):
        if (
            len(self.required_roles) != 3
            or set(self.required_roles) != ROLES
            or set(self.images) != ROLES
            or set(self.role_probes) != ROLES
        ):
            raise ValueError("plugin_image_delivery_roles")
        return self


class RoleObservation(StrictContract):
    container_id: Sha
    image_id: Digest
    image_reference: str
    state: Literal["running"]
    health: Literal["healthy"]
    health_source: Literal["docker_healthcheck", "readiness_probe"]
    probe_sha256: Sha
    probe_result: Literal["passed"]
    plugin_tree_sha256: Sha
    plugin_manifest_sha256: Sha


class DatabaseObservation(StrictContract):
    request_id: Identifier
    phase: Literal["applied"]
    plugin_id: Positive
    plugin_name: Name
    version: str
    status: Literal["enabled", "disabled", "installed"]
    manifest_sha256: Sha
    active_version_id: Positive


class HostObservation(StrictContract):
    schema_version: Literal["novusai-plugin-image-observation/v1"] = Field(
        alias="schema"
    )
    operation_id: Identifier
    release_contract_sha256: Sha
    observer_sha256: Sha
    observed_at: str
    roles: dict[Role, RoleObservation]
    database: DatabaseObservation

    @model_validator(mode="after")
    def measured(self):
        timestamp(self.observed_at)
        if (
            set(self.roles) != ROLES
            or len({row.container_id for row in self.roles.values()}) != 3
        ):
            raise ValueError("plugin_image_delivery_roles")
        return self


class DeploymentAttestation(StrictContract):
    schema_version: Literal["novusai-plugin-image-attestation/v1"] = Field(
        alias="schema"
    )
    trust_boundary: Literal["trusted-host-observer-file-v1"]
    release_contract_sha256: Sha
    release_contract_canonical_sha256: Sha
    observation_sha256: Sha
    observation_canonical_sha256: Sha
    release: ReleaseContract
    observation: HostObservation
    completed_at: str

    @model_validator(mode="after")
    def matched(self):
        release, observation = self.release, self.observation
        if self.release_contract_canonical_sha256 != canonical_sha(
            release.model_dump()
        ) or self.observation_canonical_sha256 != canonical_sha(
            observation.model_dump()
        ):
            raise ValueError("plugin_image_delivery_payload_changed")
        if (
            observation.release_contract_sha256 != self.release_contract_sha256
            or observation.operation_id != release.operation_id
            or observation.observer_sha256 != release.observer_sha256
        ):
            raise ValueError("plugin_image_delivery_operation")
        build, database = release.build_contract, observation.database
        expected_status = (
            "installed" if build.operation == "install" else build.source_status
        )
        if (
            database.request_id != build.request_id
            or database.plugin_name != build.plugin_name
            or database.version != build.target_version
            or database.status != expected_status
            or database.manifest_sha256 != build.target_manifest_sha256
            or (
                build.source_plugin_id is not None
                and database.plugin_id != build.source_plugin_id
            )
            or (
                build.operation == "rollback"
                and database.active_version_id != build.target_plugin_version_id
            )
        ):
            raise ValueError("plugin_image_delivery_database")
        for name, role in observation.roles.items():
            probe = release.role_probes[name]
            image = release.images[name]
            if (
                role.image_reference != image.reference
                or role.image_id != image.daemon_image_id
                or role.plugin_tree_sha256 != build.target_tree_sha256
                or role.plugin_manifest_sha256 != build.target_manifest_sha256
                or role.health_source != probe.health_source
                or role.probe_sha256 != probe.sha256
            ):
                raise ValueError("plugin_image_delivery_role_identity")
        if timestamp(self.completed_at) < timestamp(observation.observed_at):
            raise ValueError("plugin_image_delivery_time_order")
        return self


def strict_json(raw: bytes):
    def pairs(values):
        output = {}
        for key, value in values:
            if key in output:
                raise ValueError("plugin_image_delivery_duplicate_key")
            output[key] = value
        return output

    def invalid(_value):
        raise ValueError("plugin_image_delivery_json_number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def deployment_is_verified(request) -> bool:
    """中文: 复核持久证据和身份，旧记录/空值保持未验证。 EN: Revalidate typed evidence; legacy/null is false."""
    try:
        if (
            request.phase != "verified"
            or request.applied_at is None
            or request.deployment_completed_at is None
            or type(request.plugin_id) is not int
        ):
            return False
        attestation = DeploymentAttestation.model_validate(
            request.deployment_attestation
        )
        if (
            attestation.release.build_contract
            != BuildContract.model_validate(request.build)
            or attestation.release.build_contract.request_id != request.request_id
        ):
            return False
        database = attestation.observation.database
        completed = request.deployment_completed_at
        return (
            database.plugin_id == request.plugin_id
            and timestamp(attestation.completed_at) == completed
            and timestamp(attestation.observation.observed_at) >= request.applied_at
        )
    except Exception:
        return False
