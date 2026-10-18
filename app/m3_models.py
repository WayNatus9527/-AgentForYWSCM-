"""Strict public input contracts for counting, quality and exceptions."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator
from .clock import parse_timestamp

class Strict(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
Text=Annotated[str,Field(min_length=1,max_length=120)]
Note=Annotated[str,Field(min_length=2,max_length=1000)]
Qty=Annotated[int,Field(strict=True,ge=0,le=1_000_000)]
Positive=Annotated[int,Field(strict=True,ge=1,le=1_000_000)]

class Details(Strict):
    reason_code:Literal['supplier','transport','queue','operation','quality','data_delay','mapping','system','unknown']='unknown'
    defect_category:Literal['none','damaged','missing','wrong_item','packaging','functional','unknown']='unknown'
    responsibility_type:Literal['unknown','supplier','carrier','team','person']='unknown'
    responsibility_ref:Annotated[str,Field(max_length=120)]=''
    evidence_refs:Annotated[list[Text],Field(max_length=20)]=[]
    opinion:Note
    @model_validator(mode='after')
    def responsibility(self):
        if self.responsibility_type!='unknown' and not self.responsibility_ref:raise ValueError('请填写责任对象引用')
        if self.responsibility_type=='person' and not self.evidence_refs:raise ValueError('人员归责提议须附证据并等待人工复核')
        return self

class Scope(Strict):
    warehouse_id:Text
    sku_id:Text
class CountCreate(Strict):
    code:Text
    reason:Note
    lines:Annotated[list[Scope],Field(min_length=1,max_length=100)]
    @model_validator(mode='after')
    def unique(self):
        if len({(x.warehouse_id,x.sku_id) for x in self.lines})!=len(self.lines):raise ValueError('盘点范围不能重复')
        return self
class Action(Strict):
    expected_version:Qty
    reason:Note
class Observed(Strict):
    g:Qty
    q:Qty
    d:Qty
class CountLine(Strict):
    line_id:Text
    observed:Observed
    details:Details
class CountObservation(Action):
    lines:Annotated[list[CountLine],Field(min_length=1,max_length=100)]
class ExceptionCreate(Scope):
    object_type:Literal['stock_count','return','inspection','stock_document']
    object_id:Text
    rule:Text
    rule_version:Text
    instance:Text
    type:Literal['count_conflict','quality','supplier','transport','queue','operation','data_delay','mapping','system','unknown']
    priority:Literal['low','normal','high','critical']='normal'
    impact_quantity:Qty
    due_at:str|None=None
    details:Details
    requires_review:bool=False
    @field_validator('due_at')
    @classmethod
    def date(cls,value):return parse_timestamp(value).isoformat() if value else None
class Assign(Action):
    owner_id:Text
class Resolution(Action):
    result:Note
    details:Details
class TaskReview(Action):
    confirm_responsibility:bool=False
class InspectionStart(Scope):
    return_id:Text|None=None
    reference:Text
    quantity:Positive
    expected_balance_version:Qty
    details:Details
class InspectionComplete(Action):
    good:Qty
    bad:Qty
    expected_balance_version:Qty
    details:Details
class Restock(Action):
    quantity:Positive
    expected_balance_version:Qty
class Scrap(Strict):
    kind:Literal['scrap']
    session_id:Text
    reference:Text
    quantity:Positive
    expected_balance_version:Qty
    details:Details
class SupplierReturn(Strict):
    kind:Literal['supplier_return']
    original_id:Text
    reference:Text
    g:Qty=0
    q:Qty=0
    d:Qty=0
    expected_balance_version:Qty
    details:Details
    @model_validator(mode='after')
    def total(self):
        if not self.g+self.q+self.d:raise ValueError('退货总量须大于零')
        return self
class DispositionReview(Action):
    decision:Literal['approve','reject']
