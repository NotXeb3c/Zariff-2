"""ZariffMesh — LAN mesh orchestrator.

Ties together peer discovery, peer connections, skill sync, and
collaborative execution into a single object that ``PilotServer``
starts and stops alongside the main WebSocket server.

Lifecycle
---------
    mesh = ZariffMesh(config, executor, plugin_manager)
    await mesh.start()          # begins mDNS advertising + browsing
    ...
    await mesh.stop()           # deregisters mDNS, closes all connections

Architecture
------------
                    ┌─────────────────────────────────┐
                    │           ZariffMesh             │
                    │                                  │
    mDNS ──────────►│  PeerDiscovery                   │
                    │      │ on_peer_found              │
                    │      ▼                            │
                    │  PeerConnection pool              │
                    │      │ on_message                 │
                    │      ├──► SkillSync               │
                    │      └──► CollabExecutor          │
                    │                                  │
                    │  P2P WebSocket server (inbound)  │
                    └─────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import socket
import uuid
from typing import TYPE_CHECKING, Any

from pilot.models.gpu_utils import get_available_vram
from pilot.network.peer_connection import PeerCapabilities, PeerConnection

if TYPE_CHECKING:
    from pilot.agents.executor import Executor
    from pilot.config import NetworkConfig
    from pilot.system.plugins import PluginManager

logger = logging.getLogger("pilot.network.mesh")


class ZariffMesh:
    """Central coordinator for the LAN mesh network.

    Parameters
    ----------
    config:
        ``NetworkConfig`` from ``PilotConfig``.
    executor:
        The local ``Executor`` instance (used by ``CollabExecutor``).
    plugin_manager:
        The global ``PluginManager`` (used by ``SkillSync``).
    """

    def __init__(
        self,
        config: NetworkConfig,
        executor: Executor,
        plugin_manager: PluginManager,
    ) -> None:
        self._config = config
        self._executor = executor
        self._plugin_manager = plugin_manager
        self._instance_id = str(uuid.uuid4())[:8]

        self._connections: dict[str, PeerConnection] = {}
        self._discovery: Any = None  # PeerDiscovery (lazy import)
        self._skill_sync: Any = None  # SkillSync (lazy import)
        self._collab: Any = None  # CollabExecutor (lazy import)
        self._p2p_server: Any = None  # inbound WebSocket server
        self._running = False

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def peer_ids(self) -> list[str]:
        """IDs of all currently connected peers."""
        return [pid for pid, conn in self._connections.items() if conn.connected]

    def get_connection(self, peer_id: str) -> PeerConnection | None:
        return self._connections.get(peer_id)

    @property
    def instance_id(self) -> str:
        return self._instance_id

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the mesh: P2P server, discovery, skill sync, collab executor."""
        if self._running:
            return
        self._running = True

        # Lazy imports so missing optional deps don't crash the daemon
        from pilot.network.collab_executor import CollabExecutor
        from pilot.network.peer_discovery import PeerDiscovery
        from pilot.network.skill_sync import SkillSync

        self._skill_sync = SkillSync(self)
        self._collab = CollabExecutor(
            mesh=self,
            local_executor=self._executor,
            enabled=self._config.collab_exec_enabled,
        )

        # Start inbound P2P WebSocket server
        asyncio.create_task(self._start_p2p_server())

        # Start mDNS discovery
        self._discovery = PeerDiscovery(
            port=self._config.port,
            instance_id=self._instance_id,
        )
        self._discovery.on_peer_found = self._on_peer_found
        self._discovery.on_peer_lost = self._on_peer_lost
        await self._discovery.start()

        # Start periodic capability updates
        asyncio.create_task(self._periodic_capability_update())

        logger.info(
            "ZariffMesh started (instance=%s, port=%d)",
            self._instance_id,
            self._config.port,
        )

    async def stop(self) -> None:
        """Shut down all connections and deregister from mDNS."""
        self._running = False

        # Close all peer connections
        for conn in list(self._connections.values()):
            await conn.disconnect()
        self._connections.clear()

        # Stop discovery
        if self._discovery:
            await self._discovery.stop()

        # Stop P2P server
        if self._p2p_server:
            self._p2p_server.close()
            await self._p2p_server.wait_closed()

        logger.info("ZariffMesh stopped")

    # ── Messaging ─────────────────────────────────────────────────────────────

    async def broadcast(self, msg_type: str, payload: dict[str, Any]) -> None:
        """Send a message to all connected peers."""
        for pid in list(self.peer_ids):
            await self.send_to(pid, msg_type, payload)

    async def send_to(self, peer_id: str, msg_type: str, payload: dict[str, Any]) -> None:
        """Send a message to a specific peer."""
        conn = self._connections.get(peer_id)
        if conn and conn.connected:
            await conn.send(msg_type, payload)
        else:
            logger.warning("ZariffMesh.send_to: peer %s not connected", peer_id)

    # ── Peer events ───────────────────────────────────────────────────────────

    def _on_peer_found(self, peer_info: Any) -> None:
        """Called by PeerDiscovery when a new peer is found on the LAN."""
        asyncio.ensure_future(self._connect_to_peer(peer_info))

    def _on_peer_lost(self, peer_id: str) -> None:
        """Called by PeerDiscovery when a peer deregisters."""
        conn = self._connections.pop(peer_id, None)
        if conn:
            asyncio.ensure_future(conn.disconnect())
        logger.info("ZariffMesh: peer %s left the mesh", peer_id)

    async def _connect_to_peer(self, peer_info: Any) -> None:
        """Establish an outbound connection to a newly discovered peer."""
        peer_id = peer_info.peer_id
        if peer_id in self._connections:
            return  # already connected

        own_caps = self._build_own_capabilities()
        conn = PeerConnection(
            peer_id=peer_id,
            host=peer_info.host,
            port=peer_info.port,
            own_capabilities=own_caps,
            shared_secret=self._config.shared_secret,
            on_message=self._on_peer_message,
            on_disconnect=self._on_connection_lost,
        )
        success = await conn.connect()
        if success:
            self._connections[peer_id] = conn
            logger.info("ZariffMesh: joined mesh with peer %s", peer_id)

            # Broadcast our loaded plugins to the new peer
            if self._config.skill_sync_enabled and self._skill_sync:
                asyncio.create_task(self._sync_plugins_to_peer(peer_id))

    async def _periodic_capability_update(self) -> None:
        """Periodically refresh VRAM/CPU stats and update discovery/peers."""
        while self._running:
            await asyncio.sleep(30)  # update every 30 seconds
            if not self._running:
                break

            caps = self._build_own_capabilities()

            # 1. Update mDNS (Zeroconf) record
            if self._discovery:
                await self._discovery.update_vram(caps.vram_free, caps.has_gpu)

            # 2. Update connected peers via P2P
            await self.broadcast("peer_info", caps.__dict__)
            logger.debug("ZariffMesh: broadcasted updated capabilities (VRAM: %.1f MB)", caps.vram_free / 1024**2)

    def _on_connection_lost(self, peer_id: str) -> None:
        """Called by PeerConnection when a connection drops unexpectedly."""
        self._connections.pop(peer_id, None)
        logger.info("ZariffMesh: connection to peer %s lost", peer_id)

    # ── Message routing ───────────────────────────────────────────────────────

    async def _on_peer_message(self, peer_id: str, msg_type: str, payload: dict[str, Any]) -> None:
        """Route inbound peer messages to the appropriate handler."""
        if msg_type == "skill_sync" and self._config.skill_sync_enabled:
            if self._skill_sync:
                await self._skill_sync.handle_incoming(peer_id, payload)

        elif msg_type == "skill_ack":
            logger.info(
                "ZariffMesh: peer %s acknowledged plugin '%s' (%s)",
                peer_id,
                payload.get("name"),
                payload.get("status"),
            )

        elif msg_type == "task_delegate" and self._config.collab_exec_enabled:
            asyncio.create_task(self._handle_delegated_task(peer_id, payload))

        elif msg_type == "task_result":
            if self._collab:
                await self._collab.handle_task_result(peer_id, payload)

        else:
            logger.debug("ZariffMesh: unhandled message type '%s' from %s", msg_type, peer_id)

    # ── Inbound P2P server ────────────────────────────────────────────────────

    async def _start_p2p_server(self) -> None:
        """Start a WebSocket server that accepts inbound peer connections."""
        try:
            import websockets

            self._p2p_server = await websockets.serve(
                self._handle_inbound_peer,
                "0.0.0.0",
                self._config.port,
            )
            logger.info("ZariffMesh: P2P server listening on port %d", self._config.port)
        except Exception as exc:
            logger.error("ZariffMesh: failed to start P2P server: %s", exc)

    async def _handle_inbound_peer(self, websocket: Any) -> None:
        """Handle an inbound WebSocket connection from a peer."""
        import json

        peer_id: str | None = None
        try:
            async for raw in websocket:
                msg = json.loads(raw)
                msg_type = msg.get("type", "")
                payload = msg.get("payload", {})

                if msg_type == "peer_info":
                    if not self._secret_ok(payload):
                        try:
                            await websocket.send(
                                json.dumps(
                                    {
                                        "type": "auth_error",
                                        "payload": {"reason": "invalid or missing shared_secret"},
                                    }
                                )
                            )
                        except Exception:
                            pass
                        logger.warning(
                            "ZariffMesh: rejected inbound peer %s (%s) — invalid or missing shared_secret",
                            payload.get("instance_id", "?"),
                            websocket.remote_address,
                        )
                        break
                    peer_id = payload.get("instance_id", str(uuid.uuid4())[:8])
                    # Register a lightweight inbound connection wrapper
                    if peer_id not in self._connections:
                        own_caps = self._build_own_capabilities()
                        conn = PeerConnection(
                            peer_id=peer_id,
                            host=websocket.remote_address[0],
                            port=self._config.port,
                            own_capabilities=own_caps,
                            shared_secret=self._config.shared_secret,
                            on_message=self._on_peer_message,
                            on_disconnect=self._on_connection_lost,
                        )
                        # Attach the already-open websocket
                        conn._ws = websocket
                        conn._connected = True
                        self._connections[peer_id] = conn
                        logger.info("ZariffMesh: inbound peer %s connected", peer_id)
                    continue

                if peer_id:
                    await self._on_peer_message(peer_id, msg_type, payload)
        except Exception as exc:
            logger.debug("ZariffMesh: inbound peer connection closed: %s", exc)
        finally:
            if peer_id:
                self._connections.pop(peer_id, None)

    def _secret_ok(self, payload: dict[str, Any]) -> bool:
        """Verify the peer_info secret against the configured shared_secret.

        Fails closed: with no shared_secret configured, no peer is accepted
        and the mesh logs why. Uses a constant-time comparison so a LAN
        observer cannot use timing to brute-force the secret.
        """
        configured = self._config.shared_secret
        if not configured:
            logger.warning(
                "ZariffMesh: refusing peer connection — network.shared_secret is not set. "
                "Set the same non-empty shared_secret on every mesh peer to enable P2P."
            )
            return False
        provided = payload.get("secret", "")
        if not isinstance(provided, str) or not provided:
            return False
        return hmac.compare_digest(provided.encode("utf-8"), configured.encode("utf-8"))

    # ── Delegated task execution ──────────────────────────────────────────────

    async def _handle_delegated_task(self, peer_id: str, payload: dict[str, Any]) -> None:
        """Execute a batch of actions delegated by a peer and return results."""
        import json

        from pilot.actions import Action, ActionPlan, ActionResult
        from pilot.security.gateway import InvocationSource

        task_id = payload.get("task_id", "")
        raw_actions = payload.get("actions", [])
        raw_input = payload.get("raw_input", "")

        try:
            actions = [Action.model_validate(a) for a in raw_actions]
        except Exception as exc:
            logger.warning("ZariffMesh: failed to deserialise delegated actions: %s", exc)
            await self.send_to(peer_id, "task_result", {"task_id": task_id, "results": []})
            return

        plan = ActionPlan(actions=actions, raw_input=raw_input, explanation="Delegated by peer")

        # Never let a peer push a plan through that would require interactive
        # human approval locally — there is no human on the receiving side of
        # a mesh delegation, and the interactive confirmation gate only exists
        # on the main WebSocket path.
        if self._executor.plan_requires_confirmation(plan):
            logger.warning(
                "ZariffMesh: refusing delegated task %s from %s — requires local confirmation",
                task_id,
                peer_id,
            )
            refused = [
                ActionResult(
                    action=action,
                    success=False,
                    error="Delegated action refused: requires local interactive confirmation",
                )
                for action in actions
            ]
            serialised = [r.model_dump(mode="json") for r in refused]
            await self.send_to(peer_id, "task_result", {"task_id": task_id, "results": serialised})
            return

        results = await self._executor.execute(
            plan,
            invocation_source=InvocationSource.MESH,
            user_confirmed=False,
        )

        serialised = [r.model_dump(mode="json") for r in results]
        await self.send_to(peer_id, "task_result", {"task_id": task_id, "results": serialised})
        logger.info(
            "ZariffMesh: completed delegated task %s for peer %s (%d results)",
            task_id,
            peer_id,
            len(results),
        )

    # ── Plugin sync helpers ───────────────────────────────────────────────────

    async def _sync_plugins_to_peer(self, peer_id: str) -> None:
        """Send all locally loaded plugins to a newly connected peer."""
        if not self._skill_sync:
            return
        plugins = self._plugin_manager.list_plugins()
        for plugin in plugins:
            if plugin.get("loaded") and plugin.get("file_path"):
                await self._skill_sync.broadcast_plugin(plugin["name"], plugin["file_path"])

    def _build_own_capabilities(self) -> PeerCapabilities:
        """Build a capability advertisement for this instance."""
        try:
            import psutil

            cpu_load = psutil.cpu_percent(interval=None) / 100.0
        except Exception:
            cpu_load = 0.0

        plugins = [p["name"] for p in self._plugin_manager.list_plugins() if p.get("loaded")]
        vram_free, has_gpu = get_available_vram()

        return PeerCapabilities(
            instance_id=self._instance_id,
            hostname=socket.gethostname(),
            can_execute=self._config.collab_exec_enabled,
            cpu_load=cpu_load,
            vram_free=vram_free,
            has_gpu=has_gpu,
            plugin_names=plugins,
        )
