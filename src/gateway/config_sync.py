"""Fleet-wide convergence for request-governing configuration.

``self.projects`` and ``self._user_configs`` are hydrated once at startup and
then only ever mutated by the task that served the write. Behind the shipped
``desired_count=2`` (``infra/stack.py``, auto-scaling to 10) that makes every
config write half-applied, and unlike a stale count it is not cosmetic — both
dicts gate requests:

* ``GatewayAgent`` resolves ``self._projects[project_id]`` to find the project's
  budget limit, allowed-models list, guardrails, and rate limit. An unresolved
  project is not an error; it means *no gate at all*.
* ``self._user_configs[user_id]["allowed_models"]`` is the per-user model
  restriction, and ``cost_tracker._user_budgets`` is armed from the same row.

So a restriction an operator sets is enforced by the task that took the PUT and
ignored by the others, chosen per request by the load balancer:

    in the store: {'alice': {'allowed_models': ['claude-haiku']}}
    task A, alice asks for claude-opus: 403 model_not_allowed
    task B, alice asks for claude-opus: 200 routed

Same mechanism as ``CedarPolicyService.refresh_if_stale`` and the same shape of
fix: writes bump a shared version counter, and each instance re-reads the config
scans only when that number moves. The cost in steady state is one small
``GetItem`` per instance per window, not two table scans per request.

Region topology is already stored as one revisioned document, so it is polled
directly on the same bounded interval. That keeps routing and data-residency
rules converged without coupling a topology write to a second counter write.
"""

from __future__ import annotations

import asyncio
import logging
import time

from src.gateway.multi_region.region_config import apply_persisted_topology

logger = logging.getLogger(__name__)


class RegionTopologyUnavailable(RuntimeError):
    """The authoritative topology could not be checked safely."""


class ConfigSyncService:
    """Re-adopts fleet-wide project and user config on a version change.

    Holds the *same* dict objects ``AdminAPI`` and ``GatewayAgent`` hold and
    mutates them in place. Rebinding would put this service's converged view in
    an object nobody reads — the failure mode that made ``x or {}`` a fleet-wide
    bug in the first place.
    """

    # Matches CedarPolicyService.POLICY_SYNC_TTL_SECONDS deliberately. Both bound
    # the window in which two tasks disagree about an *enforcement* rule rather
    # than about a displayed number, so they should not be tuned apart: an
    # operator who has waited out one has waited out the other. The check is a
    # single counter GetItem, so 5s is affordable per request.
    CONFIG_SYNC_TTL_SECONDS = 5.0

    def __init__(
        self,
        projects: dict,
        user_configs: dict[str, dict],
        cost_tracker,
        persistence=None,
        policy_resolver=None,
        region_config=None,
        health_monitor=None,
        region_lock: asyncio.Lock | None = None,
    ) -> None:
        self._projects = projects
        self._user_configs = user_configs
        self._cost_tracker = cost_tracker
        self._persistence = persistence
        self._policy_resolver = policy_resolver
        self._region_config = region_config
        self._health_monitor = health_monitor
        self._region_lock = region_lock or asyncio.Lock()
        # Negative infinity, not 0: time.monotonic() has an arbitrary origin and
        # can legitimately be near 0 early in the process, which with a 0 sentinel
        # would skip the first check.
        self._last_version_check = float("-inf")
        self._known_version: int | None = None
        self._refresh_task: asyncio.Task | None = None
        self._local_generation = 0
        self._last_region_check = float("-inf")
        self._known_region_revision = (
            getattr(region_config, "revision", 0)
            if region_config is not None
            else None
        )
        self._region_refresh_task: asyncio.Task | None = None

    @property
    def region_lock(self) -> asyncio.Lock:
        """Lock shared with the local topology writer."""
        return self._region_lock

    async def refresh_if_stale(self) -> bool:
        """Adopt fleet config if another instance changed it. Returns whether it did.

        Single-flighted for the same reason the other two refreshes are: the TTL
        check straddles an await, so without it every request in a concurrent
        burst passes the check and issues its own pair of scans.
        """
        if self._persistence is None or not self._persistence.enabled:
            return False

        region_refreshed = await self._refresh_region_if_stale()
        now = time.monotonic()
        if now - self._last_version_check < self.CONFIG_SYNC_TTL_SECONDS:
            return region_refreshed

        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh(now))
        try:
            config_refreshed = await asyncio.shield(self._refresh_task)
            return region_refreshed or config_refreshed
        except Exception:
            logger.warning("Config refresh failed", exc_info=True)
            return region_refreshed

    async def _refresh_region_if_stale(self) -> bool:
        if self._region_config is None:
            return False
        loader = getattr(
            self._persistence,
            "load_region_topology_snapshot",
            None,
        )
        if loader is None:
            raise RegionTopologyUnavailable(
                "Region topology loading is not configured"
            )
        now = time.monotonic()
        if (
            now - self._last_region_check
            < self.CONFIG_SYNC_TTL_SECONDS
        ):
            return False
        if (
            self._region_refresh_task is None
            or self._region_refresh_task.done()
        ):
            self._region_refresh_task = asyncio.create_task(
                self._refresh_region(now)
            )
        try:
            return await asyncio.shield(self._region_refresh_task)
        except RegionTopologyUnavailable:
            raise
        except Exception as exc:
            logger.error("Region topology refresh failed", exc_info=True)
            raise RegionTopologyUnavailable(
                "Region topology is temporarily unavailable"
            ) from exc

    async def _refresh_region(self, now: float) -> bool:
        async with self._region_lock:
            snapshot = (
                await self._persistence.load_region_topology_snapshot()
            )
            if snapshot is None:
                self._known_region_revision = getattr(
                    self._region_config,
                    "revision",
                    0,
                )
                await self._reconcile_health_monitor()
                self._last_region_check = now
                return False

            revision = snapshot.get("revision")
            if (
                not isinstance(revision, int)
                or isinstance(revision, bool)
                or revision < 0
            ):
                raise RegionTopologyUnavailable(
                    "Region topology revision is invalid"
                )
            live_revision = getattr(self._region_config, "revision", 0)
            if revision <= live_revision:
                # A delayed read must never roll back a local commit or a newer
                # refresh. Track the live revision, not the stale poller's prior
                # observation, so the next check starts from reality.
                self._known_region_revision = live_revision
                await self._reconcile_health_monitor()
                self._last_region_check = now
                return False

            apply_persisted_topology(
                self._region_config,
                snapshot,
                preserve_health=True,
            )
            logger.info(
                "Adopting region topology revision %s -> %s",
                live_revision,
                revision,
            )
            self._known_region_revision = revision
            await self._reconcile_health_monitor()
            self._last_region_check = now
            return True

    async def _reconcile_health_monitor(self) -> None:
        reconcile = getattr(self._health_monitor, "reconcile", None)
        if callable(reconcile):
            await reconcile()

    async def _refresh(self, now: float) -> bool:
        generation = self._local_generation
        version = await self._persistence.get_config_version()
        if version is None:
            # Unreadable. Keep the config we have and retry on the next request
            # rather than advancing the clock — an outage must not buy a full
            # window of divergence.
            return False

        if version == self._known_version:
            self._last_version_check = now
            return False

        projects, configs = await asyncio.gather(
            self._persistence.load_projects_or_none(),
            self._persistence.load_user_configs_or_none(),
        )
        if projects is None or configs is None:
            # The version moved but a scan failed. Do NOT adopt the empty result:
            # that would clear every budget limit and model restriction in the
            # fleet because one scan timed out — a read failure turned into a
            # fleet-wide enforcement bypass. Leave _known_version alone so the
            # next request retries.
            logger.error(
                "Config version moved to %s but a config scan failed "
                "(projects=%s, user_configs=%s); continuing with the loaded config",
                version,
                "ok" if projects is not None else "failed",
                "ok" if configs is not None else "failed",
            )
            return False

        confirmed_version = await self._persistence.get_config_version()
        if confirmed_version is None or confirmed_version != version:
            # A write landed while the two scans were in flight. Mixing rows
            # from before and after that write would acknowledge a snapshot that
            # never existed, so retry the whole read on the next request.
            return False
        if generation != self._local_generation:
            # This process committed a local mutation while the scan was
            # running. Publishing the older scan would roll that mutation back.
            return False

        # Stored entries win; entries this instance knows and the store does not
        # survive. Same merge as bootstrap's and as the Cedar refresh's: seed-file
        # projects and users are not in DynamoDB, so replacing outright would
        # silently drop every seeded one.
        self._projects.update(projects)
        for user_id, config in configs.items():
            self._user_configs[user_id] = config

        # Adopting the dicts is not the same as arming enforcement — the same
        # distinction #89 fixed for the restart path. Limits live in
        # cost_tracker._budgets / ._user_budgets, which no dict update touches, so
        # without this the refreshed limit would be displayed and enforced by
        # nothing.
        self._register_budgets(projects, configs)

        logger.info(
            "Adopting fleet config: version %s -> %s (%d projects, %d user configs)",
            self._known_version, version, len(projects), len(configs),
        )
        self._known_version = version
        self._last_version_check = now
        return True

    def _register_budgets(self, projects: dict, configs: dict[str, dict]) -> None:
        """Arm enforcement for the limits just adopted.

        Deliberately does not touch the spend counters. Limits and spend have
        different owners: ``_bump_spend_fleet_wide`` keeps the counters fleet-wide
        already, and writing to them from a config refresh is how a read path
        reopens a closed budget gate.
        """
        for project in projects.values():
            if project.budget_limit is None and project.alert_threshold is None:
                continue
            self._cost_tracker.register_project(
                project.project_id,
                budget_limit=project.budget_limit,
                alert_threshold=project.alert_threshold,
                tenant_id=project.tenant_id,
            )
            # Only where no node exists, matching _register_persisted_budgets: a
            # real org -> team -> project hierarchy carries a tighter parent cap
            # that a flat per-project node would flatten away.
            if (
                self._policy_resolver is not None
                and project.budget_limit is not None
                and project.project_id not in self._policy_resolver._nodes
            ):
                from src.gateway.models import PolicyNode

                self._policy_resolver._nodes[project.project_id] = PolicyNode(
                    node_id=project.project_id,
                    node_type="project",
                    parent_id=None,
                    display_name=project.name,
                    limits={"budget_limit": project.budget_limit},
                )

        for user_id, config in configs.items():
            # Registered even when both limits are None, matching bootstrap:
            # clearing a limit is a deliberate act, and a config row exists
            # because someone configured the user.
            self._cost_tracker.register_user(
                user_id,
                budget_limit=config.get("budget_limit"),
                alert_threshold=config.get("alert_threshold"),
            )

    def note_local_version(self, version: int | None) -> None:
        """Record a version this instance produced by writing config itself.

        Without this the writing instance sees its own bump as a remote change on
        the next poll and re-scans to learn what it already knows.
        """
        if version is not None:
            self._known_version = version
            self._last_version_check = time.monotonic()

    def invalidate_local_config(self) -> None:
        """Force a verified snapshot after this process commits a local write."""
        self._local_generation += 1
        self._last_version_check = float("-inf")

    def note_local_region_revision(self, revision: int) -> None:
        """Record a topology revision this instance already published."""
        if (
            not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
        ):
            raise ValueError(
                "topology revision must be a non-negative integer"
            )
        self._known_region_revision = revision
        self._last_region_check = time.monotonic()
