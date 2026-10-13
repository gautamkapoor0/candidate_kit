"""Employee Attendance & Analytics API."""
import os, math
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import ObjectId

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_emp_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_emp_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_emp")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", ASCENDING)], name="attendance_status_date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_punch_in")

def now_utc(): return datetime.now(UTC).replace(microsecond=0)
def to_ms(v):
    if v is None: return None
    if v.tzinfo is None: v = v.replace(tzinfo=UTC)
    return int(v.timestamp()*1000)
def from_ms(v):
    if isinstance(v, bool) or not isinstance(v, int) or v < 100000000000 or v > 4102444800000:
        raise HTTPException(422, "timestamp must be epoch milliseconds in a valid range")
    return datetime.fromtimestamp(v/1000, UTC).replace(microsecond=0)
def utc_naive(v):
    if v is None: return None
    if v.tzinfo is None: return v.replace(tzinfo=UTC)
    return v.astimezone(UTC)
def serialize(doc):
    d = dict(doc)
    d.pop("_id", None)
    for k in ("punch_in","punch_out","created_at","at"):
        if isinstance(d.get(k), datetime): d[k] = to_ms(d[k])
    for h in d.get("history", []):
        if isinstance(h.get("at"), datetime): h["at"] = to_ms(h["at"])
        for ch in h.get("changes", {}).values():
            for k in ("from","to"):
                if isinstance(ch.get(k), datetime): ch[k] = to_ms(ch[k])
    return d
def round_half(v, places=2):
    if v is None: return None
    return float(Decimal(str(v)).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))
def shift_dt(day, hhmm):
    h,m=map(int,hhmm.split(":")); return datetime.combine(day,time(h,m),IST)
def attendance_day(ts, emp):
    local=utc_naive(ts).replace(tzinfo=UTC).astimezone(IST)
    d=local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M") < emp["shift_end"]:
        d -= timedelta(days=1)
    return d.isoformat()
def late_minutes(pin, shift_start):
    local=utc_naive(pin).astimezone(IST); start=local.replace(hour=int(shift_start[:2]), minute=int(shift_start[3:]), second=0,microsecond=0)
    delta=int((local-start).total_seconds())
    return delta//60 if delta > 600 else 0
def calc_derived(pin,pout,emp,status):
    if status in ("ABSENT","LEAVE"): return {"late_minutes":0,"work_hours":None,"overtime_minutes":0,"half_day":False}
    late=late_minutes(pin,emp["shift_start"]) if pin else 0
    if not pin or not pout: return {"late_minutes":late,"work_hours":None,"overtime_minutes":0,"half_day":False}
    seconds=(utc_naive(pout)-utc_naive(pin)).total_seconds()
    hours=round_half(seconds/3600,2)
    localin=utc_naive(pin).astimezone(IST)
    day=localin.date()
    # For overnight shifts, end occurs the following calendar day.
    end_day=day+timedelta(days=1) if emp["shift_end"] <= emp["shift_start"] else day
    end=shift_dt(end_day,emp["shift_end"]).astimezone(UTC)
    over=int((utc_naive(pout)-end).total_seconds()//60)
    return {"late_minutes":late,"work_hours":hours,"overtime_minutes":over if over>=30 else 0,"half_day":hours<4.5}
def working_days(start,end):
    n=0
    while start<=end:
        if start.weekday()<5:n+=1
        start+=timedelta(days=1)
    return n
def date_range_month(month):
    try:
        y,m=map(int,month.split("-")); first=date(y,m,1)
        last=date(y+1,1,1)-timedelta(days=1) if m==12 else date(y,m+1,1)-timedelta(days=1)
        return first,last
    except Exception: raise HTTPException(422,"month must be YYYY-MM")
def month_param(v): 
    if not re_month(v): raise HTTPException(422,"month must be YYYY-MM")
    date_range_month(v); return v
def re_month(v): return isinstance(v,str) and len(v)==7 and v[4]=="-" and v[:4].isdigit() and v[5:].isdigit() and 1<=int(v[5:])<=12
def check_date(v):
    try: return date.fromisoformat(v)
    except Exception: raise HTTPException(422,"date must be YYYY-MM-DD")
def valid_page(page,page_size):
    if page<1 or not 1<=page_size<=100: raise HTTPException(422,"page must be >=1 and page_size between 1 and 100")

class EmployeeIn(BaseModel):
    emp_code:str; name:str; email:str; department:str
    shift_start:str="09:30"; shift_end:str="18:30"; joined_on:str
    @field_validator("shift_start","shift_end")
    @classmethod
    def valid_time(cls,v):
        try:
            t=datetime.strptime(v,"%H:%M")
            if t.strftime("%H:%M")!=v: raise ValueError()
            return v
        except Exception: raise ValueError("time must be HH:MM")
    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls,v): check_date(v); return v
    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start==self.shift_end: raise ValueError("shift_start and shift_end must differ")
        return self
class PunchInIn(BaseModel):
    emp_code:str; punched_at:Optional[int]=None; status:Literal["PRESENT","WFH","ON_DUTY"]="PRESENT"
class PunchOutIn(BaseModel):
    emp_code:str; punched_at:Optional[int]=None
class RegularizeIn(BaseModel):
    status:Optional[Literal["PRESENT","WFH","ON_DUTY","ABSENT","LEAVE"]]=None
    punch_in:Optional[int]=None; punch_out:Optional[int]=None
    reason:str=Field(min_length=5,max_length=200); regularized_by:str=Field(min_length=1,max_length=50)

@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status":"ok"}
    except Exception: raise HTTPException(503,"MongoDB unavailable")

@app.post("/employees",status_code=201)
def create_employee(body:EmployeeIn):
    doc=body.model_dump(); doc["created_at"]=now_utc()
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"emp_code already exists")
    return serialize(doc)
@app.get("/employees")
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={"department":department} if department else {}
    total=db.employees.count_documents(q)
    items=[serialize(x) for x in db.employees.find(q,{"_id":0}).sort("emp_code",1).skip((page-1)*page_size).limit(page_size)]
    return {"items":items,"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    d=attendance_day(ts,emp)
    doc={"emp_code":body.emp_code,"date":d,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,
         "late_minutes":late_minutes(ts,emp["shift_start"]),"overtime_minutes":0,"half_day":False,"history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return serialize(doc)

@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    # Latest open punch-in that precedes this instant, including overnight shifts.
    rec=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$ne":None,"$lte":ts}},sort=[("punch_in",-1)])
    if not rec: raise HTTPException(404,"no punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409,"record already punched out")
    if ts<=utc_naive(rec["punch_in"]) or ts-utc_naive(rec["punch_in"])>timedelta(hours=24):
        raise HTTPException(422,"punch-out must be after punch-in and within 24 hours")
    der=calc_derived(rec["punch_in"],ts,emp,rec["status"])
    result=db.attendance_logs.find_one_and_update({"_id":rec["_id"],"punch_out":None},{"$set":{"punch_out":ts,**der}},return_document=ReturnDocument.AFTER)
    if not result: raise HTTPException(409,"record already punched out")
    return serialize(result)

@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={}
    if emp_code:q["emp_code"]=emp_code
    if status:
        if status not in STATUSES: raise HTTPException(422,"invalid status")
        q["status"]=status
    if date_from or date_to:
        if date_from and date_to and check_date(date_from)>check_date(date_to): raise HTTPException(422,"date_from must be <= date_to")
        q["date"]={}
        if date_from: check_date(date_from); q["date"]["$gte"]=date_from
        if date_to: check_date(date_to); q["date"]["$lte"]=date_to
    total=db.attendance_logs.count_documents(q)
    docs=db.attendance_logs.find(q,{"_id":0}).sort([("date",-1),("emp_code",1)]).skip((page-1)*page_size).limit(page_size)
    return {"items":[serialize(x) for x in docs],"total":total,"page":page,"page_size":page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code:str,date:str,body:RegularizeIn):
    check_date(date)
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    rec=db.attendance_logs.find_one({"emp_code":emp_code,"date":date})
    if not rec: raise HTTPException(404,"attendance record not found")
    pin=from_ms(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
    pout=from_ms(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
    status=body.status or rec["status"]
    if status in ("ABSENT","LEAVE"):
        if body.punch_in is not None or body.punch_out is not None: raise HTTPException(422,"ABSENT/LEAVE cannot have punch times")
        pin=pout=None
    else:
        if pin is None: raise HTTPException(422,"presence status requires punch_in")
        if attendance_day(pin,emp)!=date: raise HTTPException(422,"punch_in must belong to attendance date")
        if pout is not None and (pout<=pin or pout-pin>timedelta(hours=24)): raise HTTPException(422,"punch_out must be after punch_in and within 24 hours")
    der=calc_derived(pin,pout,emp,status)
    final={"status":status,"punch_in":pin,"punch_out":pout,**der}
    changes={}
    for k,v in final.items():
        old=rec.get(k, {"late_minutes":0,"overtime_minutes":0,"half_day":False}.get(k))
        if old!=v: changes[k]={"from":old,"to":v}
    if not changes: raise HTTPException(422,"request makes no changes")
    entry={"at":now_utc(),"by":body.regularized_by,"reason":body.reason,"changes":changes}
    # Optimistic compare-and-swap prevents lost history; concurrent loser receives 409.
    oldhist=rec.get("history",[])
    result=db.attendance_logs.find_one_and_update({"_id":rec["_id"],"history":oldhist,"status":rec["status"],"punch_in":rec.get("punch_in"),"punch_out":rec.get("punch_out")},
       {"$set":final,"$push":{"history":entry}},return_document=ReturnDocument.AFTER)
    if result is None: raise HTTPException(409,"record changed concurrently; retry")
    return serialize(result)

def employee_month_pipeline(emp_code,month):
    first,last=date_range_month(month)
    return [{"$match":{"emp_code":emp_code,"date":{"$gte":first.isoformat(),"$lte":last.isoformat()}}},
      {"$group":{"_id":None,"present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$gte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},2]},{"$lte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},6]}]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}},"leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},"total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},"total_overtime_minutes":{"$sum":{"$ifNull":["$overtime_minutes",0]}}}}]
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    month_param(month); first,last=date_range_month(month)
    start=max(first,check_date(emp["joined_on"]))
    wd=working_days(start,last) if start<=last else 0
    agg=list(db.attendance_logs.aggregate(employee_month_pipeline(emp_code,month)))
    a=agg[0] if agg else {}
    present=a.get("present_days",0)
    return {"emp_code":emp_code,"month":month,"working_days":wd,"present_days":present,"leave_days":a.get("leave_days",0),"late_count":a.get("late_count",0),"total_late_minutes":a.get("total_late_minutes",0),"total_overtime_minutes":a.get("total_overtime_minutes",0),"attendance_pct":round_half(present/wd*100,4) if wd else None}

@app.get("/analytics/departments/summary")
def department_summary(month:str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),department:Optional[str]=None):
    month_param(month); first,last=date_range_month(month)
    match={"date":{"$gte":first.isoformat(),"$lte":last.isoformat()}}
    pipeline=[
      {"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"emp"}},
      {"$unwind":"$emp"},{"$match":{"emp.joined_on":{"$lte":last.isoformat()},**({"emp.department":department} if department else {})}},
      {"$group":{"_id":"$emp.department","present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$gte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},2]},{"$lte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},6]}]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}},"avg_work_hours":{"$avg":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$ne":["$work_hours",None]}]},"$work_hours",None]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},"total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},"leave_count":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},"on_duty_count":{"$sum":{"$cond":[{"$eq":["$status","ON_DUTY"]},1,0]}}}},
      {"$project":{"_id":0,"department":"$_id","present_days":1,"avg_work_hours":{"$round":["$avg_work_hours",2]},"late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}}]
    # Include zero-log employees/headcount using employee-led aggregation and a lookup of monthly records.
    empmatch={"joined_on":{"$lte":last.isoformat()}}
    if department: empmatch["department"]=department
    headpipe=[{"$match":empmatch},{"$group":{"_id":"$department","headcount":{"$sum":1}}}]
    heads={x["_id"]:x["headcount"] for x in db.employees.aggregate(headpipe)}
    stats={x["department"]:x for x in db.attendance_logs.aggregate([{"$match":match}]+pipeline)}
    items=[]
    for dep,count in sorted(heads.items()):
        s=stats.get(dep,{})
        items.append({"department":dep,"headcount":count,"present_days":s.get("present_days",0),"avg_work_hours":s.get("avg_work_hours"),"late_count":s.get("late_count",0),"total_late_minutes":s.get("total_late_minutes",0),"leave_count":s.get("leave_count",0),"on_duty_count":s.get("on_duty_count",0)})
    return {"month":month,"items":items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    month_param(month); first,last=date_range_month(month)
    pipe=[{"$match":{"date":{"$gte":first.isoformat(),"$lte":last.isoformat()}}},
      {"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":{"$ifNull":["$late_minutes",0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}},
      {"$match":{"total_late_minutes":{"$gt":0}}},
      {"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"emp"}},{"$unwind":"$emp"},
      *([{"$match":{"emp.department":department}}] if department else []),
      {"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},
      {"$match":{"rank":{"$lte":limit}}},{"$sort":{"total_late_minutes":-1,"_id":1}},
      {"$project":{"_id":0,"rank":1,"emp_code":"$_id","name":"$emp.name","department":"$emp.department","total_late_minutes":1,"late_count":1}}]
    return {"month":month,"items":list(db.attendance_logs.aggregate(pipe,allowDiskUse=True))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department:str,from_date:str=Query(...,alias="from"),to_date:str=Query(...,alias="to")):
    start=check_date(from_date); end=check_date(to_date)
    if end<start or (end-start).days+1>92: raise HTTPException(422,"range must be ordered and at most 92 days")
    if not db.employees.find_one({"department":department}): raise HTTPException(404,"department not found")
    # All daily rows are generated by MongoDB $densify; headcount and rates are calculated in-pipeline.
    startdt=datetime.combine(start,time(),UTC); enddt=datetime.combine(end,time(),UTC)
    pipe=[
      {"$match":{"date":{"$gte":from_date,"$lte":to_date}}},
      {"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"e"}},{"$unwind":"$e"},
      {"$match":{"e.department":department}},
      {"$group":{"_id":"$date","present_count":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$gte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},2]},{"$lte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},6]}]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}},
      {"$project":{"_id":0,"date":"$_id","present_count":1,"late_count":1,"dt":{"$dateFromString":{"dateString":"$_id"}}}},
      {"$sort":{"dt":1}},
      {"$densify":{"field":"dt","range":{"step":1,"unit":"day","bounds":[startdt,enddt+timedelta(days=1)]}}},
      {"$set":{"date":{"$dateToString":{"date":"$dt","format":"%Y-%m-%d"}},"present_count":{"$ifNull":["$present_count",0]},"late_count":{"$ifNull":["$late_count",0]},"weekday":{"$dayOfWeek":{"date":"$dt","timezone":"UTC"}}}},
      {"$lookup":{"from":"employees","let":{"day":"$date"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$department",department]},{"$lte":["$joined_on","$$day"]}]}}},{"$count":"n"}],"as":"hc"}},
      {"$set":{"headcount":{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]},"is_working_day":{"$and":[{"$gte":["$weekday",2]},{"$lte":["$weekday",6]}]}}},
      {"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},{"$round":[{"$divide":["$present_count","$headcount"]},4]},None]}}},
      {"$setWindowFields":{"sortBy":{"dt":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},
      {"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":1,"moving_avg_7d":{"$round":["$moving_avg_7d",4]}}}
    ]
    rows=list(db.attendance_logs.aggregate(pipe,allowDiskUse=True))
    # $densify may yield no rows when there are no matching logs; generate date spine in MongoDB via a seed pipeline.
    if not rows:
        rows=list(db.employees.aggregate([
          {"$match":{"department":department}},
          {"$limit":1},
          {"$project":{"_id":0,"dt":{"$literal":startdt}}},
          {"$densify":{"field":"dt","range":{"step":1,"unit":"day","bounds":[startdt,enddt+timedelta(days=1)]}}},
          {"$set":{"date":{"$dateToString":{"date":"$dt","format":"%Y-%m-%d"}},"weekday":{"$dayOfWeek":"$dt"}}},
          {"$lookup":{"from":"employees","let":{"day":"$date"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$department",department]},{"$lte":["$joined_on","$$day"]}]}}},{"$count":"n"}],"as":"hc"}},
          {"$set":{"headcount":{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]},"is_working_day":{"$and":[{"$gte":["$weekday",2]},{"$lte":["$weekday",6]}]},"present_count":{"$literal":0},"late_count":{"$literal":0}}},
          {"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},0,None]}}},
          {"$setWindowFields":{"sortBy":{"dt":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},
          {"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":1,"moving_avg_7d":{"$round":["$moving_avg_7d",4]}}}
        ]))
    # When records exist only on some dates, the densify pipeline may start at first record. Ensure output includes requested boundaries.
    bydate={r["date"]:r for r in rows}
    if len(bydate)!=(end-start).days+1:
        # Re-run an employee-led pipeline seeded with the requested range, still keeping gap-fill/window operations in MongoDB.
        seed=[{"$match":{"department":department}},{"$limit":1},{"$project":{"dt":{"$literal":startdt}}},
          {"$densify":{"field":"dt","range":{"step":1,"unit":"day","bounds":[startdt,enddt+timedelta(days=1)]}}},
          {"$set":{"date":{"$dateToString":{"date":"$dt","format":"%Y-%m-%d"}},"weekday":{"$dayOfWeek":"$dt"}}},
          {"$lookup":{"from":"employees","let":{"day":"$date"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$department",department]},{"$lte":["$joined_on","$$day"]}]}}},{"$count":"n"}],"as":"hc"}},
          {"$lookup":{"from":"attendance_logs","let":{"day":"$date"},"pipeline":[{"$match":{"$expr":{"$eq":["$date","$$day"]}}},{"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"e"}},{"$unwind":"$e"},{"$match":{"$expr":{"$eq":["$e.department",department]}}},{"$group":{"_id":None,"present_count":{"$sum":{"$cond":[{"$and":[{"$in":["$status",list(PRESENCE)]},{"$gte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},2]},{"$lte":[{"$dayOfWeek":{"$dateFromString":{"dateString":"$date"}}},6]}]},{"$cond":[{"$eq":["$half_day",True]},0.5,1]},0]}},"late_count":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}} }],"as":"daily"}},
          {"$set":{"headcount":{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]},"is_working_day":{"$and":[{"$gte":["$weekday",2]},{"$lte":["$weekday",6]}]},"present_count":{"$ifNull":[{"$arrayElemAt":["$daily.present_count",0]},0]},"late_count":{"$ifNull":[{"$arrayElemAt":["$daily.late_count",0]},0]}}},
          {"$set":{"attendance_rate":{"$cond":[{"$and":["$is_working_day",{"$gt":["$headcount",0]}]},{"$round":[{"$divide":["$present_count","$headcount"]},4]},None]}}},
          {"$setWindowFields":{"sortBy":{"dt":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},
          {"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":1,"late_count":1,"attendance_rate":1,"moving_avg_7d":{"$round":["$moving_avg_7d",4]}}}]
        rows=list(db.employees.aggregate(seed,allowDiskUse=True))
    return {"department":department,"items":rows}

@app.get("/admin/explain/{endpoint}")
def explain(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,from_date:Optional[str]=Query(None,alias="from"),to_date:Optional[str]=Query(None,alias="to"),page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if endpoint not in {"attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"}: raise HTTPException(422,"invalid endpoint")
    if endpoint=="attendance_list":
        q={}
        if emp_code:q["emp_code"]=emp_code
        if status:q["status"]=status
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=date_from
            if date_to:q["date"]["$lte"]=date_to
        return db.command("explain",{"find":"attendance_logs","filter":q,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size}, verbosity="executionStats")
    if endpoint=="employee_monthly":
        if not emp_code or not month: raise HTTPException(422,"emp_code and month required")
        month_param(month)
        return db.command("explain",{"aggregate":"attendance_logs","pipeline":employee_month_pipeline(emp_code,month),"cursor":{}}, verbosity="executionStats")
    if endpoint=="department_summary":
        if not month: raise HTTPException(422,"month required")
        month_param(month); first,last=date_range_month(month)
        return db.command("explain",{"aggregate":"attendance_logs","pipeline":[{"$match":{"date":{"$gte":first.isoformat(),"$lte":last.isoformat()}}}],"cursor":{}}, verbosity="executionStats")
    if endpoint=="late_leaderboard":
        if not month: raise HTTPException(422,"month required")
        month_param(month); first,last=date_range_month(month)
        return db.command("explain",{"aggregate":"attendance_logs","pipeline":[{"$match":{"date":{"$gte":first.isoformat(),"$lte":last.isoformat()}}},{"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"}}}],"cursor":{}}, verbosity="executionStats")
    if not from_date or not to_date: raise HTTPException(422,"from and to required")
    check_date(from_date);check_date(to_date)
    return db.command("explain",{"aggregate":"attendance_logs","pipeline":[{"$match":{"date":{"$gte":from_date,"$lte":to_date}}}],"cursor":{}}, verbosity="executionStats")
