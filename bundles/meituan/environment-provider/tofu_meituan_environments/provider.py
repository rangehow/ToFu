"""MLP provider API v1. Current CLI sources define IDs, units and endpoints."""
from __future__ import annotations
import re
from tofu_environments import EnvironmentError, Snapshot
from tofu_environments.contracts import seal
from .workspace import WorkspaceRuntime

class MlpProvider:
    provider_id = "mlp"
    operation_timeout = 120
    def __init__(self, transport, *, workspace_runtime=None):
        self.transport = transport
        self.runtime = workspace_runtime or WorkspaceRuntime(transport)
    def _call(self, actor, spec, method, path, payload=None, params=None):
        return self.transport.request(actor, spec["project"], method, path, payload, params)
    def _numeric(self, value, *, uncertain=False):
        if not isinstance(value, (str, int)) or not str(value).isdigit() or int(value) <= 0:
            raise EnvironmentError("provider_protocol", "Platform identifier must be a positive number", uncertain=uncertain)
        return str(value)
    def _marker(self, actor, spec, operation):
        return "tofu.environment=" + actor.scope + ":" + operation + ":" + seal(spec)
    def _image(self, actor, spec, image_id):
        value = self._call(actor, spec, "GET", "/mlapi/image/" + self._numeric(image_id))
        if not isinstance(value, dict): raise EnvironmentError("provider_protocol", "Image detail is missing")
        return value
    def _detail(self, actor, spec, workspace_id):
        value = self._call(actor, spec, "GET", "/mlapi/codelab/" + self._numeric(workspace_id))
        if not isinstance(value, dict) or value.get("projectName") != spec["project"]:
            raise EnvironmentError("foreign_resource", "Workspace project does not match")
        return value
    def catalog(self, actor, project, query):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", project):
            raise EnvironmentError("invalid_project", "Invalid project identifier")
        spec = {"project": project}
        rows = self._call(actor, spec, "GET", "/mlapi/imageinfo/list", params={"keyword": query, "page": 1, "pageSize": 20, "category": "all", "onlyMe": "false", "sameGroup": "false"})
        if isinstance(rows, dict): rows = rows.get("items", rows.get("data", []))
        queues = self._call(actor, spec, "GET", "/mlapi/codelab/docker/queue", params={"projectName": project})
        if isinstance(queues, dict): queues = queues.get("items", queues.get("data", []))
        if not isinstance(rows, list) or not isinstance(queues, list):
            raise EnvironmentError("provider_protocol", "Catalog response is invalid")
        return {"images": [{"base": str(x.get("imageId", "")), "revision": str(x.get("id", "")),
                            "reference": x.get("imageAddress", x.get("imageName")), "name": x.get("imageName"),
                            "recommendations": x.get("recommends")} for x in rows[:20]],
                "queues": [{"id": x.get("name"), "resources": x.get("resource", {})} for x in queues[:100]],
                "constraints": {"scope": "personal", "context": "Dockerfile base-image extension", "memory_unit": "GiB"}}
    def validate(self, actor, spec):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", spec["project"]):
            raise EnvironmentError("invalid_project", "Invalid project identifier")
        if "use_sankuai" in spec.get("provider_options", {}) and type(spec["provider_options"]["use_sankuai"]) is not bool:
            raise EnvironmentError("invalid_option", "use_sankuai must be a boolean")
        if spec.get("provider_options", {}).keys() - {"use_sankuai", "hadoop_account"}:
            raise EnvironmentError("unsupported_option", "Unknown provider option")
        account = spec.get("provider_options", {}).get("hadoop_account")
        if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", account):
            raise EnvironmentError("account_required", "Declare the approved Hadoop account; it is not inferred from project name")
        if spec["workspace"].get("mounts") or spec["workspace"].get("ports"):
            raise EnvironmentError("unsupported_option", "MLP mounts/ports require platform options, not local Docker bindings")
        if spec["image"].get("context", ".") != "." or re.search(r"(?im)^\s*(FROM|COPY|ADD)\s", spec["image"]["instructions"]):
            raise EnvironmentError("unsupported_context", "MLP native builder accepts base-image extensions without local build context")
        if not spec["workspace"].get("queue"):
            raise EnvironmentError("queue_required", "An explicit owned queue is required")
        if spec["profile"] == "tofu_service" and (not spec["workspace"].get("startup_command") or not spec["workspace"].get("service_port")):
            raise EnvironmentError("service_configuration", "Service profile requires startup_command and service_port")
        base = self._numeric(spec["image"]["base"])
        revision = spec["image"].get("base_revision")
        if not revision:
            raise EnvironmentError("base_revision_required", "Declare both base image ID and immutable version record ID")
        detail = self._image(actor, spec, revision)
        if str(detail.get("imageId")) != base or str(detail.get("buildStatus", "")).lower() not in {"pushed", "approved"}:
            raise EnvironmentError("base_unavailable", "Approved base image/version does not match")
        queues = self._call(actor, spec, "GET", "/mlapi/codelab/docker/queue", params={"projectName": spec["project"]})
        if isinstance(queues, dict): queues = queues.get("items", queues.get("data", []))
        if not isinstance(queues, list) or not any(x.get("name") == spec["workspace"]["queue"] for x in queues):
            raise EnvironmentError("queue_unavailable", "Selected queue is not available to this project")
        return {"provider_api": 1, "base_image_id": base, "base_revision": str(revision),
                "base_address": detail.get("imageName"), "project": spec["project"], "queue": spec["workspace"]["queue"]}
    def create_image(self, actor, spec, operation_id):
        payload = {"baseImageId": int(spec["image"]["base"]),
                   "simpleImageName": spec["image"]["name"][:48] + "-" + operation_id[:12],
                   "purpose": "training", "visibleScope": [spec["project"]],
                   "hadoopAccount": spec["provider_options"]["hadoop_account"], "isBase": False,
                   "comment": self._marker(actor, spec, operation_id),
                   "components": [{"componentType": "config", "componentContent": spec["image"]["instructions"], "comment": ""}]}
        value = self._call(actor, spec, "POST", "/mlapi/image/create", payload)
        return self._numeric(value, uncertain=True)
    def start_build(self, actor, spec, image_id, operation_id, *, admission):
        image = self._image(actor, spec, image_id)
        if image.get("comment") != self._marker(actor, spec, operation_id):
            raise EnvironmentError("foreign_resource", "Image operation identity does not match")
        self._call(actor, spec, "POST", "/mlapi/image/" + image_id + "/build")
    def image_status(self, actor, spec, image_id, operation_id):
        image = self._image(actor, spec, image_id)
        if image.get("comment") != self._marker(actor, spec, operation_id):
            raise EnvironmentError("foreign_resource", "Image operation identity does not match")
        status = str(image.get("buildStatus", "")).lower()
        if status in {"pushed", "approved"}: return Snapshot("ready")
        if status in {"failed", "build_failed", "push_failed", "check_failed", "revoked", "exit", "external_failure"}: return Snapshot("failed")
        return Snapshot("waiting")
    def create_workspace(self, actor, spec, image_id, operation_id):
        image = self._image(actor, spec, image_id)
        if image.get("comment") != self._marker(actor, spec, operation_id) or self.image_status(actor, spec, image_id, operation_id).state != "ready":
            raise EnvironmentError("image_not_ready", "Exact built image is not ready")
        address = image.get("imageName")
        if not isinstance(address, str) or not address or any(c.isspace() for c in address):
            raise EnvironmentError("provider_protocol", "Built image address is missing")
        allowed = self._call(actor, spec, "GET", "/mlapi/codelab/image/list", params={"keyword": spec["image"]["name"]})
        if isinstance(allowed, dict): allowed = allowed.get("items", allowed.get("data", []))
        if not isinstance(allowed, list) or not any(x.get("imageAddress", x.get("imageName")) == address for x in allowed):
            raise EnvironmentError("image_not_codelab_ready", "Built image is not yet admitted for CodeLab")
        running = self._call(actor, spec, "GET", "/mlapi/codelab/list", params={"page": 1, "pageSize": 100, "status": 20, "isOwn": "true"})
        if not isinstance(running, dict) or not isinstance(running.get("items"), list):
            raise EnvironmentError("provider_protocol", "Running workspace count could not be verified")
        # Preserve the current official CLI guard; do not bypass its platform policy.
        if int(running.get("totalCount", len(running["items"]))) >= 2:
            raise EnvironmentError("workspace_limit", "Official provider running workspace limit reached")
        w = spec["workspace"]; use_sankuai = bool(spec.get("provider_options", {}).get("use_sankuai", False))
        payload = {"id": 0, "name": w["name"] + "-" + operation_id[:8], "ideType": "CodeLab", "projectName": spec["project"],
                   "useSankuai": use_sankuai, "scope": 0, "password": "", "needGpu": bool(w.get("gpu_count", 0)),
                   "gpuNumber": w.get("gpu_count", 0), "queue": w["queue"], "gpuType": w.get("gpu_type", ""),
                   "image": address, "cpuNumber": w["cpu"], "memory": w["memory_gib"], "resourceConfigType": 0,
                   "otherParam": "pc.env.TOFU_ENV_OPERATION=" + operation_id + "\npc.env.TOFU_ENV_OWNER=" + actor.scope,
                   "condaPath": "", "componentVersion": ""}
        if not use_sankuai and not re.match(r"^root.*(hltx_training_cluster|tx_training_cluster)", w["queue"], re.I):
            payload["dolphinfsUserType"] = 0
        value = self._call(actor, spec, "PUT", "/mlapi/codelab", payload)
        if isinstance(value, dict): value = value.get("id")
        return self._numeric(value, uncertain=True)
    def activate_workspace(self, actor, spec, workspace_id, operation_id):
        detail = self._detail(actor, spec, workspace_id)
        self._require_owned(actor, detail, operation_id)
        if int(detail.get("status", 0)) == 50: raise EnvironmentError("workspace_failed", "Workspace startup failed")
        if int(detail.get("status", 0)) != 20: return False
        for ide in ([1, 3] if spec["profile"] == "tofu_service" else [3 if spec["workspace"].get("ide") == "vscode" else 1]):
            self._call(actor, spec, "POST", f"/mlapi/codelab/{workspace_id}/ide/{ide}/0")
        if spec["profile"] == "tofu_service":
            self.runtime.start(actor, spec, detail, operation_id)
        return True
    def _owns(self, actor, detail, operation_id):
        params = detail.get("otherParam", "")
        if not isinstance(params, str): return False
        for key, expected in (("TOFU_ENV_OPERATION", operation_id), ("TOFU_ENV_OWNER", actor.scope)):
            values = re.findall(r"(?:^|\s)pc\.env\." + key + r"=([^\s]+)(?=\s|$)", params)
            if values != [expected]: return False
        return True
    def _require_owned(self, actor, detail, operation_id):
        if not self._owns(actor, detail, operation_id):
            raise EnvironmentError("foreign_resource", "Workspace operation/owner markers do not match")
    def workspace_status(self, actor, spec, workspace_id, operation_id):
        detail = self._detail(actor, spec, workspace_id)
        self._require_owned(actor, detail, operation_id)
        if int(detail.get("status", 0)) == 50: return Snapshot("failed")
        if int(detail.get("status", 0)) != 20: return Snapshot("waiting")
        wanted = 3 if spec["workspace"].get("ide") == "vscode" else 1
        if not any(int(x.get("devTool", 0)) == wanted and int(x.get("status", -1)) == 0 for x in detail.get("ideStatus", [])):
            return Snapshot("waiting")
        if spec["profile"] == "tofu_service" and not self.runtime.ready(actor, spec, detail): return Snapshot("waiting")
        return Snapshot("ready", {"kind": "codelab", "workspace_id": workspace_id,
            "url": self.transport.origin + "/ml/#/codeLab/detail/" + workspace_id,
            **({"service_url": self.runtime.service_url(spec, detail)} if spec["profile"] == "tofu_service" else {})})
    def reconcile(self, actor, record):
        spec = record["plan"]["spec"]; operation = record["operation_id"]
        phase = record.get("uncertain_phase", record["phase"])
        if phase == "creating_image":
            rows = self._call(actor, spec, "GET", "/mlapi/image/list", params={"type": "1", "manual": "false", "sort": "createTime,desc", "query": spec["image"]["name"], "onlyMe": "true", "page": 1, "pageSize": 100})
            if isinstance(rows, dict): rows = rows.get("items", [])
            matches = [x for x in rows or [] if x.get("comment") == self._marker(actor, spec, operation)]
            if len(matches) == 1: return {"phase": "image_created", "image_id": self._numeric(matches[0]["id"])}
        elif phase == "starting_build" and record.get("image_id"):
            image = self._image(actor, spec, record["image_id"])
            if image.get("comment") != self._marker(actor, spec, operation): return None
            state = str(image.get("buildStatus", "")).lower()
            if state in {"accepted", "building", "built", "pushing", "pushed", "approved"}: return {"phase": "building"}
        elif phase == "creating_workspace":
            rows = self._call(actor, spec, "GET", "/mlapi/codelab/list", params={"query": spec["workspace"]["name"], "isOwn": "true", "page": 1, "pageSize": 100})
            matches = [x for x in (rows or {}).get("items", []) if x.get("projectName") == spec["project"] and self._owns(actor, x, operation)]
            if len(matches) == 1: return {"phase": "workspace_created", "workspace_id": self._numeric(matches[0]["id"])}
        elif phase == "activating_workspace" and record.get("workspace_id"):
            if self.workspace_status(actor, spec, record["workspace_id"], operation).state == "ready": return {"phase": "activating"}
        return None
