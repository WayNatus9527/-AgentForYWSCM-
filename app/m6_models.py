"""Explicit, bounded analysis configuration and controlled tool selection."""
from typing import Annotated,Literal
from pydantic import Field,model_validator
from .m4_models import Strict,Text
from .m5_models import Timestamp,Reason,Positive
from .clock import parse_timestamp

class Calendar(Strict):
    version: Literal['shift-v1']='shift-v1'
    weekdays: Annotated[list[Annotated[int,Field(strict=True,ge=0,le=6)]],Field(min_length=1,max_length=7)]=[0,1,2,3,4]
    start_hour: Annotated[int,Field(strict=True,ge=0,le=23)]=9
    end_hour: Annotated[int,Field(strict=True,ge=1,le=24)]=18
    @model_validator(mode='after')
    def check(self):
        if self.start_hour>=self.end_hour or len(set(self.weekdays))!=len(self.weekdays):raise ValueError('工作日历时段或星期重复')
        return self

class Analyze(Strict):
    warehouse_id: Text
    sku_id: Text|None=None
    supplier_id: Text|None=None
    connection_id: Text|None=None
    start_at: Timestamp
    end_at: Timestamp
    waiting_hours: Annotated[int,Field(strict=True,ge=1,le=8760)]=24
    registration_hours: Annotated[int,Field(strict=True,ge=1,le=8760)]=24
    min_samples: Annotated[int,Field(strict=True,ge=1,le=100)]=5
    calendar: Calendar=Calendar()
    @model_validator(mode='after')
    def times(self):
        days=(parse_timestamp(self.end_at)-parse_timestamp(self.start_at)).total_seconds()/86400
        if not 0<days<=366:raise ValueError('分析期间须大于零且不超过366天')
        return self

class Correct(Strict):
    expected_event_id: Text
    occurred_at: Timestamp|None
    planned_at: Timestamp|None
    time_quality: Literal['unknown','reported','verified']
    reason: Reason
    @model_validator(mode='after')
    def quality(self):
        if self.occurred_at is None and self.time_quality!='unknown':raise ValueError('缺少实际时间必须保持未知')
        return self

class Improvement(Strict):
    reason: Reason

class Retry(Strict):
    expected_attempts: Annotated[int,Field(strict=True,ge=0,le=1000000)]
    reason: Reason

class Tool(Strict):
    tool: Literal['explain_report','explain_replenishment','track_purchase','calculate_replenishment','clarify','unsupported']
    identity: Text|None=None
    @model_validator(mode='after')
    def identity_required(self):
        if self.tool not in ('clarify','unsupported') and not self.identity:raise ValueError('须明确选择业务记录')
        if self.tool in ('clarify','unsupported') and self.identity:raise ValueError('未知意图不接受业务ID')
        return self

class Ask(Strict):
    question: Annotated[str,Field(min_length=2,max_length=1000)]
    selected_tool: Literal['explain_report','explain_replenishment','track_purchase','calculate_replenishment']
    identity: Text
