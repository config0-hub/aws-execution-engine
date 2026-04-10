"""OrderBundler — assembles the full env var dict for SOPS encryption.

This is the higher-level class that knows about orders, jobs, trace IDs,
and credential sources. It merges everything into a flat dict, then hands
it off to sops.repackage_order which stays generic.
"""

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from src.common import sops


@dataclass
class OrderBundler:
    """Assembles and repackages an order's execution bundle.

    Collects credentials (SSM, Secrets Manager), user env vars,
    engine introspection fields, and the callback URL into a single
    flat dict, then delegates encryption to sops.repackage_order.
    """

    # Identifiers — injected as env vars for worker introspection
    run_id: str = ""
    order_id: str = ""
    order_num: str = ""
    trace_id: str = ""
    flow_id: str = ""

    # Commands
    cmds: List[str] = field(default_factory=list)

    # Credential sources
    env_vars: Dict[str, str] = field(default_factory=dict)
    ssm_values: Dict[str, str] = field(default_factory=dict)
    secret_values: Dict[str, str] = field(default_factory=dict)

    # Callback
    callback_url: str = ""

    def build_env(self) -> Dict[str, str]:
        """Assemble the full env var dict for encryption.

        Merge order: env_vars -> ssm_values -> secret_values -> engine fields.
        Later sources overwrite earlier ones on key collision.
        """
        merged: Dict[str, str] = {}

        # User-provided env vars
        merged.update(self.env_vars)

        # Credentials from SSM / Secrets Manager
        merged.update(self.ssm_values)
        merged.update(self.secret_values)

        # Commands (JSON-serialized list)
        if self.cmds:
            merged["CMDS"] = json.dumps(self.cmds)

        # Callback URL
        if self.callback_url:
            merged["CALLBACK_URL"] = self.callback_url

        # Engine introspection fields
        merged["TRACE_ID"] = self.trace_id
        merged["RUN_ID"] = self.run_id
        merged["ORDER_ID"] = self.order_id
        merged["ORDER_NUM"] = self.order_num
        merged["FLOW_ID"] = self.flow_id

        return merged

    def secret_sources(self) -> List[str]:
        """Return sorted list of SSM/Secrets Manager key names that were fetched."""
        return sorted(list(self.ssm_values.keys()) + list(self.secret_values.keys()))

    def repackage(self, code_dir: str, sops_key: Optional[str] = None) -> str:
        """Build the env dict, encrypt with SOPS, and write to code_dir.

        Also writes secrets.src manifest listing credential source keys.
        Returns the code_dir path.
        """
        import os

        env = self.build_env()
        result_dir = sops.repackage_order(code_dir, env, sops_key=sops_key)

        # Write secrets.src — list of credential keys fetched
        sources = self.secret_sources()
        if sources:
            secrets_src = os.path.join(code_dir, "secrets.src")
            with open(secrets_src, "w") as f:
                for path in sources:
                    f.write(f"{path}\n")

        return result_dir
