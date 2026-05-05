"""IntentExecutor: pure translation from typed intents to candidate device commands.

Stage 1 of the two-stage execution pipeline (Story 8.2). Translation is
intentionally separate from policy enforcement: every command produced here is
still subject to ``PolicyGuard.authorize_and_dispatch()`` before any adapter
``send_command()`` is invoked.

Hold intents (``BatteryIntentAction.hold``, ``EVChargerIntentAction.hold``)
produce no command — the executor silently skips them.
"""

from __future__ import annotations

import structlog

from open_ems.core import BatteryState, DeviceRole, EVChargerState, SystemSnapshot
from open_ems.core.commands import (
    CommandOrigin,
    DeviceCommand,
    SetBatteryChargeRateCommand,
    SetBatteryDischargeRateCommand,
    SetEVChargingRateCommand,
    StopEVChargingCommand,
)
from open_ems.engine.result import EvaluationResult
from open_ems.engine.rules.battery_control import BatteryIntent, BatteryIntentAction
from open_ems.engine.rules.ev_scheduling import EVChargerIntent, EVChargerIntentAction

logger = structlog.get_logger(__name__)


class IntentExecutor:
    """Translates typed decision-engine intents into candidate ``DeviceCommand`` objects."""

    def translate(
        self,
        result: EvaluationResult,
        snapshot: SystemSnapshot,
    ) -> list[DeviceCommand]:
        commands: list[DeviceCommand] = []
        for intent in result.intents:
            if isinstance(intent, BatteryIntent):
                cmd = self._translate_battery(intent, snapshot)
            elif isinstance(intent, EVChargerIntent):
                cmd = self._translate_ev(intent, snapshot)
            else:
                logger.warning(
                    "unhandled_intent_type",
                    intent_type=type(intent).__name__,
                    component="engine",
                )
                continue
            if cmd is not None:
                commands.append(cmd)
        return commands

    def _translate_battery(
        self,
        intent: BatteryIntent,
        snapshot: SystemSnapshot,
    ) -> DeviceCommand | None:
        if intent.action is BatteryIntentAction.hold:
            return None
        if not isinstance(snapshot.battery, BatteryState):
            logger.debug(
                "intent_skipped_battery_unavailable",
                action=intent.action.value,
                component="engine",
            )
            return None
        rate_kw = intent.target_power_kw if intent.target_power_kw is not None else 0.0
        if intent.action is BatteryIntentAction.charge:
            return SetBatteryChargeRateCommand(
                device_id=snapshot.battery.device_id,
                device_role=DeviceRole.battery,
                origin=CommandOrigin.decision_engine,
                rate_kw=rate_kw,
            )
        if intent.action is BatteryIntentAction.discharge:
            return SetBatteryDischargeRateCommand(
                device_id=snapshot.battery.device_id,
                device_role=DeviceRole.battery,
                origin=CommandOrigin.decision_engine,
                rate_kw=rate_kw,
            )
        raise ValueError(f"Unhandled BatteryIntentAction: {intent.action!r}")

    def _translate_ev(
        self,
        intent: EVChargerIntent,
        snapshot: SystemSnapshot,
    ) -> DeviceCommand | None:
        if intent.action is EVChargerIntentAction.hold:
            return None
        if not isinstance(snapshot.ev_charger, EVChargerState):
            logger.debug(
                "intent_skipped_ev_unavailable",
                action=intent.action.value,
                component="engine",
            )
            return None
        if intent.action is EVChargerIntentAction.charge:
            rate_kw = (
                intent.target_charge_rate_kw if intent.target_charge_rate_kw is not None else 0.0
            )
            return SetEVChargingRateCommand(
                device_id=snapshot.ev_charger.device_id,
                device_role=DeviceRole.ev_charger,
                origin=CommandOrigin.decision_engine,
                rate_kw=rate_kw,
            )
        if intent.action is EVChargerIntentAction.stop:
            return StopEVChargingCommand(
                device_id=snapshot.ev_charger.device_id,
                device_role=DeviceRole.ev_charger,
                origin=CommandOrigin.decision_engine,
            )
        raise ValueError(f"Unhandled EVChargerIntentAction: {intent.action!r}")
