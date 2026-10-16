"""Shared cabinet fault wording for the controller and both desktop banners."""
from collections.abc import Mapping
from typing import Any

from .models import FaultSource


def cabinet_fault_messages(status: Mapping[str, Any]) -> list[str]:
    flags = int(status.get("FaultSourceFlags") or 0)
    flags |= int(FaultSource.PLC_LATCHED) if status.get("FaultLatched") else 0
    flags |= int(FaultSource.PS1_COMM) if status.get("PS1CommFault") else 0
    flags |= int(FaultSource.PS1_DEVICE) if status.get("PS1DeviceFault") else 0
    messages = []
    if flags & FaultSource.PLC_LATCHED:
        messages.append(f"PLC控制故障已锁存（FaultCode={status.get('FaultCode', '—')}）")
    if flags & FaultSource.PS1_COMM:
        messages.append(f"PLC↔PS1通信故障（MB_Status=0x{int(status.get('PS1MbStatus') or 0):04X}）")
    if flags & FaultSource.PS1_DEVICE:
        messages.append(f"PS1设备故障（DeviceStatus=0x{int(status.get('PS1DeviceStatus') or 0):04X}）")
    if flags & FaultSource.HOST_SERVER:
        messages.append(f"PLC主机通信服务故障（ServerStatus=0x{int(status.get('HostServerStatus') or 0):04X}）")
    if status.get("PS1Fault") and not flags & (FaultSource.PS1_COMM | FaultSource.PS1_DEVICE):
        # Older/custom snapshots may lack the two independent fault bits.
        messages.append("PS1故障（来源未细分，不能判为PLC锁存故障）")
    return messages
