"""Enforce server-owned namespaces for opt-in LiteLLM response caching."""

import os
import re
import secrets
from typing import Any

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger


class CacheNamespacePolicy(CustomLogger):
    """Strip caller namespaces and require a hashed virtual-key identity."""

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> dict:
        cache_control = data.get("cache")
        if not isinstance(cache_control, dict):
            return data

        # Caller cache.namespace outranks metadata.redis_namespace upstream, so
        # strip it on every dict cache object before deciding whether to opt in.
        cache_control.pop("namespace", None)
        if cache_control.get("use-cache") is not True:
            return data

        api_key = getattr(user_api_key_dict, "api_key", None)
        if not api_key:
            # JWT and other authenticated paths can leave api_key unset. Refuse
            # reuse rather than falling through to the shared config namespace.
            raise HTTPException(
                status_code=401,
                detail="Response caching requires an authenticated virtual API key",
            )

        # In 1.81.14, virtual sk- keys reach hooks as lowercase SHA-256 hashes;
        # master-key auth remains raw. Generated master keys are also 64 hex
        # characters, so reject the configured master before checking the shape.
        master_key = os.environ.get("LITELLM_MASTER_KEY")
        if not master_key:
            raise HTTPException(
                status_code=403,
                detail="Response caching cannot verify virtual-key identity",
            )
        is_hashed_virtual_key = (
            isinstance(api_key, str)
            and re.fullmatch(r"[0-9a-f]{64}", api_key) is not None
        )
        is_master_key = isinstance(api_key, str) and secrets.compare_digest(
            api_key, master_key
        )
        if is_master_key or not is_hashed_virtual_key:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Response caching requires a virtual key; "
                    "master-key auth cannot opt in to tenant namespaces"
                ),
            )

        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            data["metadata"] = metadata
        # batch_redis_requests normally overwrites this with the same value. Keep
        # it as a safe fallback because that pinned hook swallows other failures.
        metadata["redis_namespace"] = f"litellm:{api_key}:{call_type}"
        return data


cache_namespace_policy = CacheNamespacePolicy()
