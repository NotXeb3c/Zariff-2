"""Background workers for non-blocking operations."""

from __future__ import annotations

import logging

import psutil
from PySide6.QtCore import QThread, Signal

from core.agent import Agent
from core.events import AgentState, bus
from config.settings import get_settings
from voice import stt, tts
from voice.wakeword import strip_wake_word

logger = logging.getLogger("zariff.workers")


class AgentWorker(QThread):
    finished_response = Signal(str)
    failed = Signal(str)

    def __init__(self, agent: Agent, text: str, speak: bool = False) -> None:
        super().__init__()
        self.agent = agent
        self.text = text
        self.speak_response = speak

    def run(self) -> None:
        try:
            response = self.agent.process(self.text)
            if self.speak_response and response:
                bus.set_state(AgentState.SPEAKING)
                settings = get_settings()
                tts.speak(response, rate=settings.tts_rate, volume=settings.tts_volume)
            self.finished_response.emit(response)
        except Exception as e:
            logger.exception("Agent worker failed")
            self.failed.emit(str(e))
        finally:
            bus.set_state(AgentState.IDLE)


class ListenWorker(QThread):
    transcribed = Signal(str)
    failed = Signal(str)

    def run(self) -> None:
        try:
            bus.set_state(AgentState.LISTENING)
            settings = get_settings()
            text = stt.listen_from_mic(settings.whisper_model, settings.microphone_index)
            _, text = strip_wake_word(text)
            self.transcribed.emit(text)
        except Exception as e:
            logger.exception("Listen failed")
            self.failed.emit(str(e))
        finally:
            bus.set_state(AgentState.IDLE)


class StatsWorker(QThread):
    updated = Signal(dict)

    def run(self) -> None:
        try:
            cpu = psutil.cpu_percent(interval=0.3)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage("C:\\" if __import__("sys").platform == "win32" else "/")
            net = psutil.net_if_stats()
            online = any(s.isup for s in net.values()) if net else True
            battery = None
            if hasattr(psutil, "sensors_battery"):
                b = psutil.sensors_battery()
                if b:
                    battery = {"percent": b.percent, "plugged": b.power_plugged}
            gpu = "N/A"
            try:
                import wmi  # type: ignore
                c = wmi.WMI()
                gpus = c.Win32_VideoController()
                if gpus:
                    gpu = gpus[0].Name or "N/A"
            except Exception:
                pass
            self.updated.emit({
                "cpu": cpu,
                "ram": mem.percent,
                "ram_used_gb": round(mem.used / (1024**3), 1),
                "ram_total_gb": round(mem.total / (1024**3), 1),
                "disk": disk.percent,
                "gpu": gpu,
                "online": online,
                "battery": battery,
            })
        except Exception as e:
            logger.debug("Stats update failed: %s", e)
