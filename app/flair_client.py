import base64
import logging
import os
import shutil
import subprocess
import time
import uuid
from typing import Any

import httpx
from cryptography.hazmat.primitives.asymmetric import ed25519

logger = logging.getLogger(__name__)


def get_flair_agent_id() -> str:
    return os.environ.get("FLAIR_AGENT_ID", "visual-memory-vault")


def get_flair_target() -> str:
    return (
        os.environ.get("FLAIR_TARGET")
        or os.environ.get("FLAIR_URL")
        or "http://127.0.0.1:19926"
    )


def _get_key_bytes(agent_id: str | None = None) -> tuple[str, bytes] | None:
    """Retrieve raw 32-byte Ed25519 private key seed and the effective agent ID."""
    aid = agent_id or get_flair_agent_id()

    # 1. Direct explicit keyfile env var
    keyfile_env = os.environ.get("FLAIR_KEYFILE")
    if keyfile_env:
        exp_path = os.path.expanduser(keyfile_env)
        if os.path.exists(exp_path):
            try:
                with open(exp_path, "rb") as f:
                    content = f.read().strip()
                    if len(content) == 32:
                        return aid, content
                    raw = base64.b64decode(content)
                    if len(raw) == 32:
                        return aid, raw
            except Exception as e:
                logger.warning("Failed to read FLAIR_KEYFILE %s: %s", exp_path, e)

    # 2. Base64 private key env var
    key_b64 = os.environ.get("FLAIR_PRIVATE_KEY_B64")
    if key_b64:
        try:
            raw = base64.b64decode(key_b64.strip())
            if len(raw) == 32:
                return aid, raw
        except Exception as e:
            logger.warning("Failed to decode FLAIR_PRIVATE_KEY_B64: %s", e)

    # 3. Check ~/.flair/keys/<aid>.key
    key_dir = os.path.expanduser("~/.flair/keys")
    key_path = os.path.join(key_dir, f"{aid}.key")
    if os.path.exists(key_path):
        try:
            with open(key_path, "rb") as f:
                content = f.read().strip()
                if len(content) == 32:
                    return aid, content
                raw = base64.b64decode(content)
                if len(raw) == 32:
                    return aid, raw
        except Exception as e:
            logger.warning("Failed to read key file %s: %s", key_path, e)

    # 4. Fallback to local.key if present
    local_key_path = os.path.join(key_dir, "local.key")
    if os.path.exists(local_key_path):
        try:
            with open(local_key_path, "rb") as f:
                content = f.read().strip()
                if len(content) == 32:
                    return "local", content
                raw = base64.b64decode(content)
                if len(raw) == 32:
                    return "local", raw
        except Exception as e:
            logger.warning("Failed to read local fallback key: %s", e)

    return None


def _sign_header(
    method: str, path: str, agent_id: str | None = None
) -> tuple[str, str] | None:
    """Generates (effective_agent_id, Authorization header) for Flair REST."""
    key_info = _get_key_bytes(agent_id)
    if not key_info:
        logger.debug(
            "No valid 32-byte key found for agent %s", agent_id or get_flair_agent_id()
        )
        return None
    effective_agent, raw_key = key_info
    try:
        priv_key = ed25519.Ed25519PrivateKey.from_private_bytes(raw_key)
        ts = str(int(time.time() * 1000))
        nonce = str(uuid.uuid4())
        payload = f"{effective_agent}:{ts}:{nonce}:{method}:{path}".encode()
        sig = base64.b64encode(priv_key.sign(payload)).decode("utf-8")
        return effective_agent, f"TPS-Ed25519 {effective_agent}:{ts}:{nonce}:{sig}"
    except Exception as e:
        logger.warning("Failed to sign FLAIR request: %s", e)
        return None


def store_memory(
    subject: str,
    content: str,
    tags: list[str] | None = None,
    durability: str = "persistent",
    visibility: str | None = None,
) -> dict[str, Any]:
    """Store a memory row in FLAIR for the agent."""
    mem_id = str(uuid.uuid4())
    path = f"/Memory/{mem_id}"
    target = get_flair_target()
    sign_result = _sign_header("PUT", path)

    if sign_result:
        effective_agent, auth = sign_result
        try:
            url = f"{target.rstrip('/')}{path}"
            body: dict[str, Any] = {
                "id": mem_id,
                "agentId": effective_agent,
                "subject": subject,
                "content": content,
                "durability": durability,
            }
            if tags:
                body["tags"] = tags
            if visibility:
                body["visibility"] = visibility

            with httpx.Client(timeout=15.0) as client:
                res = client.put(url, json=body, headers={"Authorization": auth})
                if res.is_success:
                    return {
                        "status": "success",
                        "id": mem_id,
                        "output": res.text,
                        "subject": subject,
                    }
                else:
                    return {
                        "status": "error",
                        "message": f"HTTP {res.status_code}: {res.text}",
                    }
        except Exception as e:
            logger.warning("Direct REST store_memory error: %s", e)
            return {"status": "error", "message": f"REST store failed: {e}"}

    # Fallback to local CLI if flair is installed
    flair_bin = shutil.which("flair")
    if flair_bin:
        aid = get_flair_agent_id()
        cmd = [
            flair_bin,
            "memory",
            "add",
            "--agent",
            aid,
            "--subject",
            subject,
            "--content",
            content,
            "--durability",
            durability,
        ]
        if tags:
            cmd.extend(["--tags", ",".join(tags)])
        if visibility:
            cmd.extend(["--visibility", visibility])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                return {
                    "status": "error",
                    "message": res.stderr.strip() or res.stdout.strip(),
                }
            return {
                "status": "success",
                "output": res.stdout.strip(),
                "subject": subject,
            }
        except Exception as err:
            return {"status": "error", "message": f"FLAIR CLI store failed: {err}"}

    return {
        "status": "error",
        "message": "No valid FLAIR authentication key or CLI available",
    }


def search_memories(query: str, limit: int = 5) -> dict[str, Any]:
    """Search FLAIR memory using semantic search."""
    path = "/SemanticSearch"
    target = get_flair_target()
    sign_result = _sign_header("POST", path)

    if sign_result:
        effective_agent, auth = sign_result
        try:
            url = f"{target.rstrip('/')}{path}"
            body = {"q": query, "limit": limit, "agentId": effective_agent}
            with httpx.Client(timeout=15.0) as client:
                res = client.post(url, json=body, headers={"Authorization": auth})
                if res.is_success:
                    return {"status": "success", "results": res.text}
                else:
                    return {
                        "status": "error",
                        "message": f"HTTP {res.status_code}: {res.text}",
                    }
        except Exception as e:
            logger.warning("Direct REST search_memories error: %s", e)
            return {"status": "error", "message": f"REST search failed: {e}"}

    flair_bin = shutil.which("flair")
    if flair_bin:
        aid = get_flair_agent_id()
        cmd = [
            flair_bin,
            "memory",
            "search",
            "--agent",
            aid,
            "--q",
            query,
            "--limit",
            str(limit),
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                return {
                    "status": "error",
                    "message": res.stderr.strip() or res.stdout.strip(),
                }
            return {"status": "success", "results": res.stdout.strip()}
        except Exception as err:
            return {"status": "error", "message": f"FLAIR CLI search failed: {err}"}

    return {
        "status": "error",
        "message": "No valid FLAIR authentication key or CLI available",
    }


def list_memories(limit: int = 20) -> dict[str, Any]:
    """List stored memories in FLAIR."""
    path = "/Memory"
    target = get_flair_target()
    sign_result = _sign_header("GET", path)

    if sign_result:
        _effective_agent, auth = sign_result
        try:
            url = f"{target.rstrip('/')}{path}"
            with httpx.Client(timeout=15.0) as client:
                res = client.get(url, headers={"Authorization": auth})
                if res.is_success:
                    return {"status": "success", "memories": res.text}
                else:
                    return {
                        "status": "error",
                        "message": f"HTTP {res.status_code}: {res.text}",
                    }
        except Exception as e:
            logger.warning("Direct REST list_memories error: %s", e)
            return {"status": "error", "message": f"REST list failed: {e}"}

    flair_bin = shutil.which("flair")
    if flair_bin:
        aid = get_flair_agent_id()
        cmd = [flair_bin, "memory", "list", "--agent", aid]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                return {
                    "status": "error",
                    "message": res.stderr.strip() or res.stdout.strip(),
                }
            return {"status": "success", "memories": res.stdout.strip()}
        except Exception as err:
            return {"status": "error", "message": f"FLAIR CLI list failed: {err}"}

    return {
        "status": "error",
        "message": "No valid FLAIR authentication key or CLI available",
    }
