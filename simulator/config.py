from datetime import datetime, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from . import VERSION

SCENARIOS = ('normal', 'supplier_delay', 'warehouse_congestion', 'transport_exception',
             'demand_spike', 'slow_sales', 'returns_surge', 'data_latency', 'mixed')
FAULTS = ('duplicate', 'reorder', 'delay', 'loss_redelivery', 'rate_limit', 'disconnect',
          'timeout_after_success', 'crash_before_commit', 'crash_after_commit')


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Calendar(Strict):
    version: Literal['calendar-1'] = 'calendar-1'
    weekdays: list[Annotated[int, Field(ge=0, le=6)]] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    start_hour: Annotated[int, Field(ge=0, le=23)] = 9
    end_hour: Annotated[int, Field(ge=1, le=24)] = 18

    @model_validator(mode='after')
    def valid(self):
        if not self.weekdays or len(set(self.weekdays)) != len(self.weekdays) or self.end_hour <= self.start_hour:
            raise ValueError('Calendar requires distinct working days and a positive shift')
        return self


class Scenario(Strict):
    generator_version: Literal[VERSION] = VERSION
    scenario_version: Literal['1'] = '1'
    scenario: Literal[SCENARIOS] = 'normal'
    purpose: Literal['development', 'regression', 'validation'] = 'development'
    seed: Annotated[int, Field(ge=0, le=2**63-1)] = 20261017
    start_time: str = '2026-10-01T09:00:00+08:00'
    timezone: str = 'Asia/Shanghai'
    sku_count: Annotated[int, Field(ge=1, le=10000)] = 4
    warehouse_count: Annotated[int, Field(ge=2, le=10)] = 2
    tenant_count: Annotated[int, Field(ge=2, le=10)] = 2
    order_count: Annotated[int, Field(ge=1, le=100000)] = 12
    supplier_lead_days: Annotated[int, Field(ge=1, le=90)] = 2
    sales_distribution: Literal['uniform', 'peaked', 'long_tail'] = 'uniform'
    return_rate: Annotated[float, Field(ge=0, le=1)] = 0.2
    inspection_rate: Annotated[float, Field(ge=0, le=1)] = 0.25
    quality_units_per_hour: Annotated[int, Field(ge=1, le=100000)] = 20
    shipping_units_per_hour: Annotated[int, Field(ge=1, le=100000)] = 50
    calendar: Calendar = Field(default_factory=Calendar)
    faults: list[Literal[FAULTS]] = Field(default_factory=list)
    declared_anomalies: list[Literal['oversell', 'cross_tenant', 'transport_shortage']] = Field(
        default_factory=lambda: ['oversell', 'cross_tenant'])

    @field_validator('timezone')
    @classmethod
    def timezone_valid(cls, value):
        ZoneInfo(value)
        return value

    @field_validator('start_time')
    @classmethod
    def time_valid(cls, value):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Start time requires an explicit timezone offset')
        return parsed.isoformat()

    @model_validator(mode='after')
    def references(self):
        if len(set(self.faults)) != len(self.faults):
            raise ValueError('Fault policies must be unique')
        if self.order_count < self.sku_count:
            raise ValueError('Each SKU requires at least one related order')
        if self.scenario in ('transport_exception', 'mixed') and 'transport_shortage' not in self.declared_anomalies:
            raise ValueError('Transport shortage must be declared as an intentional anomaly')
        return self


def profile(name, *, purpose='development', seed=20261017, **overrides):
    params = {'scenario': name, 'purpose': purpose, 'seed': seed}
    if name in ('transport_exception', 'mixed'):
        params['declared_anomalies'] = ['oversell', 'cross_tenant', 'transport_shortage']
    if name == 'mixed':
        params['faults'] = list(FAULTS)
    elif name == 'data_latency':
        params['faults'] = ['delay', 'reorder', 'loss_redelivery']
    return Scenario.model_validate(params | overrides)


def working_finish(start, hours, calendar, timezone):
    """Consume capacity only inside configured shifts, including weekends."""
    current = start.astimezone(ZoneInfo(timezone))
    seconds = hours * 3600
    while True:
        if current.weekday() not in calendar.weekdays or current.hour >= calendar.end_hour:
            current = (current + timedelta(days=1)).replace(hour=calendar.start_hour, minute=0, second=0, microsecond=0)
            continue
        shift_start = current.replace(hour=calendar.start_hour, minute=0, second=0, microsecond=0)
        current = max(current, shift_start)
        end = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=calendar.end_hour)
        available = (end - current).total_seconds()
        if seconds <= available:
            return current + timedelta(seconds=seconds)
        seconds -= available
        current = end
