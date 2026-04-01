import subprocess
import json
import os
import traceback
import requests as http_requests
from datetime import datetime
from collections import defaultdict
from functools import wraps
from flask import Flask, jsonify, request, render_template_string, session, redirect, url_for
from simple_salesforce import Salesforce

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "kpi-dashboard-secret-2026")

from config import SF_ALIAS

# ── KPI Constants ─────────────────────────────────────────────────────────────

SEGMENT_POINTS = {"A": 10, "Ar": 8, "B": 5, "C": 2, "D": 0}

BOARD_DECELERATORS = {
    "Africa":           {"CPaaS": 0.4, "SaaS": 0.8},
    "Asia and Pacific": {"CPaaS": 0.2, "SaaS": 0.6},
    "Eurasia":          {"CPaaS": 1.0, "SaaS": 1.0},
    "Europe":           {"CPaaS": 0.3, "SaaS": 0.5},
    "India":            {"CPaaS": 0.2, "SaaS": 0.6},
    "LatAm":            {"CPaaS": 0.2, "SaaS": 0.6},
    "MENA":             {"CPaaS": 0.5, "SaaS": 0.7},
    "North America":    {"CPaaS": 0.5, "SaaS": 0.5},
    "Global":           {"CPaaS": 1.0, "SaaS": 1.0},
}

PROJECT_SIZE_POINTS = [
    (0,   14,  2),
    (15,  34,  5),
    (35,  74,  10),
    (75,  99,  15),
    (100, 149, 20),
    (150, 349, 50),
    (350, float("inf"), 100),
]

KPI_TARGET = 71
KPI_CAP = 130

# ── Auth helpers ───────────────────────────────────────────────────────────────

def get_cli_token():
    """Fetch access token + instance URL from the SF CLI."""
    sf_cmd = os.path.expandvars(r"%APPDATA%\npm\sf.cmd")
    result = subprocess.run(
        [sf_cmd, "org", "display", "-o", SF_ALIAS, "--json"],
        capture_output=True, text=True
    )
    data = json.loads(result.stdout)
    if "result" not in data:
        msg = data.get("message") or data.get("name") or str(data)
        raise RuntimeError(f"SF CLI error: {msg}. Run: sf login -o {SF_ALIAS}")
    return data["result"]["accessToken"], data["result"]["instanceUrl"]

def resolve_identity():
    """
    Use the SF CLI token to identify the currently logged-in user.
    Stores user_id, user_name, access_token, instance_url in the session.
    """
    access_token, instance_url = get_cli_token()
    resp = http_requests.get(
        f"{instance_url}/services/oauth2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"}
    )
    resp.raise_for_status()
    identity = resp.json()
    session["user_id"]      = identity["user_id"]
    session["user_name"]    = identity.get("display_name") or identity.get("name", "Unknown")
    session["access_token"] = access_token
    session["instance_url"] = instance_url

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not authenticated", "auth_required": True}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def get_sf():
    """Build Salesforce connection from session token."""
    return Salesforce(
        instance_url=session["instance_url"],
        session_id=session["access_token"]
    )

# ── KPI Helpers ────────────────────────────────────────────────────────────────

def get_quarter_bounds(year, quarter):
    bounds = {
        1: (datetime(year, 1, 1),  datetime(year, 3, 31)),
        2: (datetime(year, 4, 1),  datetime(year, 6, 30)),
        3: (datetime(year, 7, 1),  datetime(year, 9, 30)),
        4: (datetime(year, 10, 1), datetime(year, 12, 31)),
    }
    return bounds[quarter]

def delivery_pts_by_hours(hrs):
    for lo, hi, pts in PROJECT_SIZE_POINTS:
        if lo <= hrs <= hi:
            return pts
    return 0

def dc_to_dc_pts(proj_name):
    """
    Returns fixed points for DC to DC migration projects, or None if not applicable.
    [DC to DC SaaS] = 7 pts, [DC to DC CPaaS] = 4 pts
    """
    name_lower = proj_name.lower()
    if "[dc to dc saas]" in name_lower:
        return 7
    if "[dc to dc cpaas]" in name_lower or "[dc to dc cpaaS]" in name_lower:
        return 4
    return None

def is_retainer(proj_name):
    return "[retainer]" in proj_name.lower()

def is_ps(name):
    return "professional services" in name.lower()

def deal_type_for(opp_name, deal_type_raw, product_family=None):
    if is_ps(opp_name):
        return "SaaS"
    # Product family (from OpportunityLineItem) always wins — SF Deal_Type__c is often stale
    if product_family and "saas" in product_family.lower():
        return "SaaS"
    if "saas" in (deal_type_raw or "").lower():
        return "SaaS"
    return "CPaaS"


def get_product_family_map(sf, opp_ids):
    """For opps with null Deal_Type__c, fetch OpportunityLineItem product families.
    Returns {opp_id: 'SaaS'|'CPaaS'} only for opps where a product family is found."""
    if not opp_ids:
        return {}
    result = {}
    for i in range(0, len(opp_ids), 200):
        chunk = ",".join(f"'{x}'" for x in opp_ids[i:i+200])
        rows = sf.query_all(
            f"SELECT OpportunityId, PricebookEntry.Product2.Family "
            f"FROM OpportunityLineItem "
            f"WHERE OpportunityId IN ({chunk}) "
            f"AND PricebookEntry.Product2.Family != null"
        )["records"]
        for r in rows:
            oid = r["OpportunityId"]
            family = (r.get("PricebookEntry") or {}).get("Product2", {}).get("Family") or ""
            if oid not in result:
                result[oid] = family
            elif "saas" in family.lower():
                result[oid] = family  # SaaS wins
    return result

def acq_pts(segment, opp_type, board, deal_type):
    base = SEGMENT_POINTS.get(segment, 0)
    if "upsell" in (opp_type or "").lower():
        key = next((k for k in BOARD_DECELERATORS if k.lower() in (board or "").lower()), None)
        if key:
            base *= BOARD_DECELERATORS[key].get(deal_type, 1.0)
    return base

def get_logged_hours(sf, project_id, user_id):
    tasks = sf.query_all(f"""
        SELECT Id FROM MPM4_BASE__Milestone1_Task__c
        WHERE MPM4_BASE__Project_Lookup__c = '{project_id}'
    """)["records"]
    if not tasks:
        return 0.0, 0.0, []
    task_ids = ",".join(f"'{t['Id']}'" for t in tasks)
    entries = sf.query_all(f"""
        SELECT MPM4_BASE__Hours__c, CreatedById, CreatedBy.Name
        FROM MPM4_BASE__Milestone1_Time__c
        WHERE MPM4_BASE__Project_Task__c IN ({task_ids})
    """)["records"]
    my_hrs = sum(e["MPM4_BASE__Hours__c"] or 0 for e in entries if e["CreatedById"] == user_id)
    others = {}
    for e in entries:
        if e["CreatedById"] != user_id:
            n = e["CreatedBy"]["Name"]
            others[n] = others.get(n, 0) + (e["MPM4_BASE__Hours__c"] or 0)
    return my_hrs, sum(others.values()), list(others.keys())


def get_logged_hours_in_quarter(sf, project_id, user_id, qs, qe):
    """Get hours logged on a project filtered to the quarter (used for retainer projects)."""
    tasks = sf.query_all(f"""
        SELECT Id FROM MPM4_BASE__Milestone1_Task__c
        WHERE MPM4_BASE__Project_Lookup__c = '{project_id}'
    """)["records"]
    if not tasks:
        return 0.0, 0.0, []
    task_ids = ",".join(f"'{t['Id']}'" for t in tasks)
    entries = sf.query_all(f"""
        SELECT MPM4_BASE__Hours__c, CreatedById, CreatedBy.Name
        FROM MPM4_BASE__Milestone1_Time__c
        WHERE MPM4_BASE__Project_Task__c IN ({task_ids})
        AND MPM4_BASE__Date__c >= {qs} AND MPM4_BASE__Date__c <= {qe}
    """)["records"]
    my_hrs = sum(e["MPM4_BASE__Hours__c"] or 0 for e in entries if e["CreatedById"] == user_id)
    others = {}
    for e in entries:
        if e["CreatedById"] != user_id:
            n = (e.get("CreatedBy") or {}).get("Name", "Other")
            others[n] = others.get(n, 0) + (e["MPM4_BASE__Hours__c"] or 0)
    return my_hrs, sum(others.values()), list(others.keys())


def get_services_sales_hours_bulk(sf, opp_ids, user_id):
    """
    Batch-fetch hours logged on 'Service Sales and Planning' milestone tasks
    for a list of opportunity IDs.
    Returns dict: {opp_id: (my_hrs, other_hrs, other_names)}
    """
    if not opp_ids:
        return {}

    # Step 1: all projects for all opps
    opp_proj_map = defaultdict(list)
    for i in range(0, len(opp_ids), 200):
        chunk = ",".join(f"'{o}'" for o in opp_ids[i:i+200])
        for p in sf.query_all(f"""
            SELECT Id, MPM4_BASE__Opportunity__c
            FROM MPM4_BASE__Milestone1_Project__c
            WHERE MPM4_BASE__Opportunity__c IN ({chunk})
        """)["records"]:
            opp_proj_map[p["MPM4_BASE__Opportunity__c"]].append(p["Id"])

    all_proj_ids = [pid for pids in opp_proj_map.values() for pid in pids]
    if not all_proj_ids:
        return {o: (0.0, 0.0, []) for o in opp_ids}

    proj_opp_map = {pid: oid for oid, pids in opp_proj_map.items() for pid in pids}

    # Step 2: Service Sales & Planning tasks for those projects
    task_proj_map = {}
    for i in range(0, len(all_proj_ids), 200):
        chunk = ",".join(f"'{p}'" for p in all_proj_ids[i:i+200])
        for t in sf.query_all(f"""
            SELECT Id, MPM4_BASE__Project_Lookup__c
            FROM MPM4_BASE__Milestone1_Task__c
            WHERE MPM4_BASE__Project_Lookup__c IN ({chunk})
            AND MPM4_BASE__Project_Milestone__r.Name LIKE '%Service%Sales%'
        """)["records"]:
            task_proj_map[t["Id"]] = t["MPM4_BASE__Project_Lookup__c"]

    if not task_proj_map:
        return {o: (0.0, 0.0, []) for o in opp_ids}

    # Step 3: time entries on those tasks
    hours_by_opp = defaultdict(lambda: {"my": 0.0, "others": {}})
    all_task_ids = list(task_proj_map.keys())
    for i in range(0, len(all_task_ids), 200):
        chunk = ",".join(f"'{t}'" for t in all_task_ids[i:i+200])
        for e in sf.query_all(f"""
            SELECT MPM4_BASE__Hours__c, CreatedById, CreatedBy.Name,
                   MPM4_BASE__Project_Task__c
            FROM MPM4_BASE__Milestone1_Time__c
            WHERE MPM4_BASE__Project_Task__c IN ({chunk})
        """)["records"]:
            tid = e["MPM4_BASE__Project_Task__c"]
            oid = proj_opp_map.get(task_proj_map.get(tid))
            if not oid:
                continue
            hrs = e["MPM4_BASE__Hours__c"] or 0
            if e["CreatedById"] == user_id:
                hours_by_opp[oid]["my"] += hrs
            else:
                n = (e.get("CreatedBy") or {}).get("Name", "Other")
                hours_by_opp[oid]["others"][n] = hours_by_opp[oid]["others"].get(n, 0) + hrs

    result = {}
    for oid in opp_ids:
        d = hours_by_opp.get(oid, {"my": 0.0, "others": {}})
        result[oid] = (d["my"], sum(d["others"].values()), list(d["others"].keys()))
    return result


def get_additional_acq_opps(sf, user_id, qs, qe):
    """
    Find Closed Won opportunities in the quarter where the user logged time on
    Service Sales & Planning tasks but is NOT the named SE on the opportunity.
    """
    entries = sf.query_all(f"""
        SELECT MPM4_BASE__Project_Task__c
        FROM MPM4_BASE__Milestone1_Time__c
        WHERE CreatedById = '{user_id}'
        AND MPM4_BASE__Project_Task__c != null
    """)["records"]

    raw_task_ids = list({e["MPM4_BASE__Project_Task__c"] for e in entries})
    if not raw_task_ids:
        return []

    # Keep only tasks under Service Sales & Planning milestone
    ss_task_ids = []
    for i in range(0, len(raw_task_ids), 200):
        chunk = ",".join(f"'{t}'" for t in raw_task_ids[i:i+200])
        ss_task_ids.extend(
            t["Id"] for t in sf.query_all(f"""
                SELECT Id FROM MPM4_BASE__Milestone1_Task__c
                WHERE Id IN ({chunk})
                AND MPM4_BASE__Project_Milestone__r.Name LIKE '%Service%Sales%'
            """)["records"]
        )
    if not ss_task_ids:
        return []

    # Projects from those tasks
    proj_ids = set()
    for i in range(0, len(ss_task_ids), 200):
        chunk = ",".join(f"'{t}'" for t in ss_task_ids[i:i+200])
        proj_ids.update(
            t["MPM4_BASE__Project_Lookup__c"]
            for t in sf.query_all(f"""
                SELECT MPM4_BASE__Project_Lookup__c
                FROM MPM4_BASE__Milestone1_Task__c
                WHERE Id IN ({chunk})
                AND MPM4_BASE__Project_Lookup__c != null
            """)["records"]
        )
    if not proj_ids:
        return []

    # Opportunity IDs from those projects
    opp_ids = set()
    for i in range(0, len(list(proj_ids)), 200):
        chunk = ",".join(f"'{p}'" for p in list(proj_ids)[i:i+200])
        opp_ids.update(
            p["MPM4_BASE__Opportunity__c"]
            for p in sf.query_all(f"""
                SELECT MPM4_BASE__Opportunity__c
                FROM MPM4_BASE__Milestone1_Project__c
                WHERE Id IN ({chunk})
                AND MPM4_BASE__Opportunity__c != null
            """)["records"]
        )
    if not opp_ids:
        return []

    # Exclude opps where user is already the named SE
    my_opp_ids = {
        o["Id"] for o in sf.query_all(f"""
            SELECT Id FROM Opportunity WHERE Sales_Engineer__c = '{user_id}'
        """)["records"]
    }
    candidate_ids = [o for o in opp_ids if o not in my_opp_ids]
    if not candidate_ids:
        return []

    additional = []
    for i in range(0, len(candidate_ids), 200):
        chunk = ",".join(f"'{o}'" for o in candidate_ids[i:i+200])
        additional.extend(sf.query_all(f"""
            SELECT Id, Name, Account.Name, Type, Customer_Segment__c,
                   Board__c, Opportunity_Owner_Board__c, Deal_Type__c
            FROM Opportunity
            WHERE Id IN ({chunk})
            AND StageName = 'Closed Won'
            AND CloseDate >= {qs} AND CloseDate <= {qe}
        """)["records"])
    return additional


def get_retainer_projects(sf, user_id, qs, qe):
    """
    Returns active retainer projects (name contains [Retainer]) where the user
    logged time in the quarter. Retainer delivery points are awarded every quarter,
    not on project completion.
    """
    # Step 1: task IDs from this user's time entries in the quarter
    entries = sf.query_all(f"""
        SELECT MPM4_BASE__Project_Task__c
        FROM MPM4_BASE__Milestone1_Time__c
        WHERE CreatedById = '{user_id}'
        AND MPM4_BASE__Date__c >= {qs} AND MPM4_BASE__Date__c <= {qe}
        AND MPM4_BASE__Project_Task__c != null
    """)["records"]

    task_ids = list({e["MPM4_BASE__Project_Task__c"] for e in entries})
    if not task_ids:
        return []

    # Step 2: project IDs from those tasks
    proj_ids = set()
    for i in range(0, len(task_ids), 200):
        chunk = ",".join(f"'{t}'" for t in task_ids[i:i+200])
        tasks = sf.query_all(f"""
            SELECT MPM4_BASE__Project_Lookup__c
            FROM MPM4_BASE__Milestone1_Task__c
            WHERE Id IN ({chunk})
            AND MPM4_BASE__Project_Lookup__c != null
        """)["records"]
        proj_ids.update(t["MPM4_BASE__Project_Lookup__c"] for t in tasks)

    if not proj_ids:
        return []

    # Step 3: fetch retainer projects (not Terminated)
    results = []
    proj_list = list(proj_ids)
    for i in range(0, len(proj_list), 200):
        chunk = ",".join(f"'{p}'" for p in proj_list[i:i+200])
        recs = sf.query_all(f"""
            SELECT Id, Name, MPM4_BASE__Status__c, MPM4_BASE__Opportunity__c,
                   MPM4_BASE__Opportunity__r.Name, Retainer_Monthly_Hours__c
            FROM MPM4_BASE__Milestone1_Project__c
            WHERE Id IN ({chunk})
            AND MPM4_BASE__Status__c != 'Terminated'
            AND Name LIKE '%[Retainer]%'
        """)["records"]
        results.extend(recs)

    return results


def get_projects_by_time_entries(sf, user_id, qs, qe):
    """
    Find completed projects where the user has time entries but is NOT the SE on the opp.
    Returns list of project records (same shape as the main delivery query).
    """
    # Step 1: all task IDs from time entries created by this user
    entries = sf.query_all(f"""
        SELECT MPM4_BASE__Project_Task__c
        FROM MPM4_BASE__Milestone1_Time__c
        WHERE CreatedById = '{user_id}'
        AND MPM4_BASE__Project_Task__c != null
    """)["records"]
    task_ids = list({e["MPM4_BASE__Project_Task__c"] for e in entries})
    if not task_ids:
        return []

    # Step 2: get project IDs from those tasks (batch in 200s to stay under SOQL limits)
    proj_ids = set()
    for i in range(0, len(task_ids), 200):
        chunk = ",".join(f"'{t}'" for t in task_ids[i:i+200])
        tasks = sf.query_all(f"""
            SELECT MPM4_BASE__Project_Lookup__c
            FROM MPM4_BASE__Milestone1_Task__c
            WHERE Id IN ({chunk})
            AND MPM4_BASE__Project_Lookup__c != null
        """)["records"]
        proj_ids.update(t["MPM4_BASE__Project_Lookup__c"] for t in tasks)

    if not proj_ids:
        return []

    # Step 3: fetch completed projects from those IDs (no date filter — we filter by opp CloseDate in Step 5)
    results = []
    proj_list = list(proj_ids)
    for i in range(0, len(proj_list), 200):
        chunk = ",".join(f"'{p}'" for p in proj_list[i:i+200])
        recs = sf.query_all(f"""
            SELECT Id, Name, MPM4_BASE__Status__c, MPM4_BASE__Opportunity__c,
                   MPM4_BASE__Opportunity__r.Name, Scoped_Hours_PSS__c, Date_Completed__c
            FROM MPM4_BASE__Milestone1_Project__c
            WHERE Id IN ({chunk})
            AND MPM4_BASE__Status__c = 'Completed'
        """)["records"]
        results.extend(recs)

    # Step 4: get the opp IDs where I am the SE, then exclude in Python
    my_opps = sf.query_all(f"""
        SELECT Id FROM Opportunity WHERE Sales_Engineer__c = '{user_id}'
    """)["records"]
    my_opp_ids = {o["Id"] for o in my_opps}

    # Step 5: only keep projects linked to Closed Won opps
    # Quarter rule: opp closed in quarter OR project completed in quarter (whichever is later)
    linked_opp_ids = list({p["MPM4_BASE__Opportunity__c"] for p in results if p.get("MPM4_BASE__Opportunity__c")})
    closed_won_ids = set()       # opp closed this quarter
    closed_won_any_ids = set()   # opp closed any time (for projects that completed this quarter)
    for i in range(0, len(linked_opp_ids), 200):
        chunk = ",".join(f"'{x}'" for x in linked_opp_ids[i:i+200])
        rows = sf.query_all(f"SELECT Id, CloseDate FROM Opportunity WHERE Id IN ({chunk}) AND StageName = 'Closed Won'")["records"]
        for r in rows:
            if r.get("CloseDate") and r["CloseDate"] <= qe:
                closed_won_any_ids.add(r["Id"])   # closed on or before end of quarter
            if r.get("CloseDate") and qs <= r["CloseDate"] <= qe:
                closed_won_ids.add(r["Id"])        # closed in this quarter specifically

    return [
        p for p in results
        if p.get("MPM4_BASE__Opportunity__c") not in my_opp_ids
        and (
            p.get("MPM4_BASE__Opportunity__c") in closed_won_ids          # opp closed this quarter
            or (p.get("MPM4_BASE__Opportunity__c") in closed_won_any_ids  # opp closed any time, project completed this quarter
                and qs <= (p.get("Date_Completed__c") or "") <= qe)
            or (not p.get("MPM4_BASE__Opportunity__c")                    # no linked opp (e.g. DC to DC): gate on Date_Completed__c
                and qs <= (p.get("Date_Completed__c") or "") <= qe)
        )
    ]

# ── KPI Calculation ───────────────────────────────────────────────────────────

def calculate_kpi(sf, user_id, year, quarter):
    q_start, q_end = get_quarter_bounds(year, quarter)
    qs = q_start.strftime("%Y-%m-%d")
    qe = q_end.strftime("%Y-%m-%d")

    # ── Acquisition ───────────────────────────────────────────────────────────
    my_opps = sf.query_all(f"""
        SELECT Id, Name, StageName, Account.Name, Type, Customer_Segment__c,
               Board__c, Opportunity_Owner_Board__c, Deal_Type__c
        FROM Opportunity
        WHERE Sales_Engineer__c = '{user_id}'
        AND StageName = 'Closed Won'
        AND CloseDate >= {qs} AND CloseDate <= {qe}
        ORDER BY CloseDate DESC
    """)["records"]

    my_opp_id_set = {o["Id"] for o in my_opps}
    additional_opps = get_additional_acq_opps(sf, user_id, qs, qe)
    all_opps = my_opps + [o for o in additional_opps if o["Id"] not in my_opp_id_set]

    # Batch-fetch Service Sales & Planning hours for all opps at once
    ss_hours = get_services_sales_hours_bulk(sf, [o["Id"] for o in all_opps], user_id)

    # Fetch product family for all opps — SaaS product family always overrides Deal_Type__c
    pf_map = get_product_family_map(sf, [o["Id"] for o in all_opps])

    acquisition = []
    for o in all_opps:
        seg   = o.get("Customer_Segment__c") or ""
        otype = o.get("Type") or ""
        board = o.get("Opportunity_Owner_Board__c") or o.get("Board__c") or ""
        dt    = deal_type_for(o.get("Name", ""), o.get("Deal_Type__c"), pf_map.get(o["Id"]))
        base  = acq_pts(seg, otype, board, dt)

        my_ss, other_ss, other_ss_names = ss_hours.get(o["Id"], (0.0, 0.0, []))
        total_ss = my_ss + other_ss

        if my_ss == 0:
            my_pct = 0.0
            pts    = 0.0
        elif total_ss > 0:
            my_pct = my_ss / total_ss
            pts    = round(base * my_pct, 2)
        else:
            my_pct = 1.0
            pts    = round(base, 2)

        notes = []
        if my_ss == 0:
            notes.append("No SS&P hours logged")
        if "upsell" in otype.lower():
            notes.append(f"Upsell decel ({dt})")
        if not seg:
            notes.append("No segment")
        if total_ss > 0 and other_ss > 0:
            notes.append(f"Shared {round(my_pct * 100)}%")

        acquisition.append({
            "name":           o.get("Name", ""),
            "account":        (o.get("Account") or {}).get("Name", ""),
            "segment":        seg,
            "type":           otype,
            "board":          board,
            "deal_type":      dt,
            "base_pts":       round(base, 2),
            "my_ss_hrs":      round(my_ss, 2),
            "other_ss_hrs":   round(other_ss, 2),
            "other_ss_names": other_ss_names,
            "my_ss_pct":      round(my_pct * 100, 0),
            "pts":            pts,
            "assisted":       o["Id"] not in my_opp_id_set,
            "notes":          "; ".join(notes),
        })

    # ── Delivery ──────────────────────────────────────────────────────────────
    # Primary: projects where I am the SE on the opp
    # Projects where opp closed this quarter (project may have completed earlier)
    my_projects_by_opp_close = sf.query_all(f"""
        SELECT Id, Name, MPM4_BASE__Status__c, MPM4_BASE__Opportunity__c,
               MPM4_BASE__Opportunity__r.Name, Scoped_Hours_PSS__c
        FROM MPM4_BASE__Milestone1_Project__c
        WHERE MPM4_BASE__Status__c = 'Completed'
        AND MPM4_BASE__Opportunity__c IN (
            SELECT Id FROM Opportunity WHERE Sales_Engineer__c = '{user_id}'
            AND StageName = 'Closed Won'
            AND CloseDate >= {qs} AND CloseDate <= {qe}
        )
    """)["records"]
    # Projects completed this quarter where opp closed this quarter or earlier
    # (if opp closes in a future quarter, delivery belongs to that future quarter)
    my_projects_by_proj_close = sf.query_all(f"""
        SELECT Id, Name, MPM4_BASE__Status__c, MPM4_BASE__Opportunity__c,
               MPM4_BASE__Opportunity__r.Name, Scoped_Hours_PSS__c
        FROM MPM4_BASE__Milestone1_Project__c
        WHERE MPM4_BASE__Status__c = 'Completed'
        AND Date_Completed__c >= {qs} AND Date_Completed__c <= {qe}
        AND MPM4_BASE__Opportunity__c IN (
            SELECT Id FROM Opportunity WHERE Sales_Engineer__c = '{user_id}'
            AND StageName = 'Closed Won'
            AND CloseDate <= {qe}
        )
    """)["records"]
    # Merge, deduplicate by project ID
    seen_my_proj = {p["Id"] for p in my_projects_by_opp_close}
    my_projects = my_projects_by_opp_close + [p for p in my_projects_by_proj_close if p["Id"] not in seen_my_proj]

    # Secondary: projects where I logged time but am not the SE (or no opp)
    assisted_projects = get_projects_by_time_entries(sf, user_id, qs, qe)

    # Merge, deduplicate by project ID — exclude retainer projects (handled separately)
    seen_proj_ids = {p["Id"] for p in my_projects}
    all_projects = my_projects + [p for p in assisted_projects if p["Id"] not in seen_proj_ids]
    all_projects = [p for p in all_projects if not is_retainer(p.get("Name", ""))]

    opp_ids = list({p["MPM4_BASE__Opportunity__c"] for p in all_projects if p.get("MPM4_BASE__Opportunity__c")})
    dt_map = {}
    if opp_ids:
        id_str = ",".join(f"'{i}'" for i in opp_ids)
        opps_raw = sf.query_all(f"SELECT Id, Name, Deal_Type__c FROM Opportunity WHERE Id IN ({id_str})")["records"]
        del_pf_map = get_product_family_map(sf, [o["Id"] for o in opps_raw])
        for o in opps_raw:
            dt_map[o["Id"]] = deal_type_for(o.get("Name", ""), o.get("Deal_Type__c"), del_pf_map.get(o["Id"]))

    opp_proj_map = defaultdict(list)
    for p in all_projects:
        opp_proj_map[p["MPM4_BASE__Opportunity__c"]].append(p)

    delivery_by_opp = []
    seen_opps = {}
    for p in all_projects:
        opp_id = p["MPM4_BASE__Opportunity__c"]
        opp_name = (p.get("MPM4_BASE__Opportunity__r") or {}).get("Name", "") or "(No Opportunity)"

        # Deal type: from opp if available, else detect from project name
        if opp_id and opp_id in dt_map:
            dt = dt_map[opp_id]
        else:
            proj_name_lower = p.get("Name", "").lower()
            dt = "SaaS" if "saas" in proj_name_lower or "professional services" in proj_name_lower else "CPaaS"

        proj_name = p.get("Name", "")
        fixed_pts = dc_to_dc_pts(proj_name)

        scoped = p.get("Scoped_Hours_PSS__c")
        hrs_bracket = scoped if scoped is not None else (4.0 if dt == "SaaS" else 2.0)
        if fixed_pts is not None:
            scoped_label = f"DC to DC fixed ({fixed_pts} pts)"
        elif scoped is not None:
            scoped_label = f"{scoped}h"
        else:
            scoped_label = f"{dt} fallback ({hrs_bracket}h)"

        my_hrs, other_hrs, other_names = get_logged_hours(sf, p["Id"], user_id)
        total_hrs = my_hrs + other_hrs

        if my_hrs == 0:
            pts = 0.0
            my_pct = 0.0
        elif total_hrs == 0:
            pts = 0.0
            my_pct = 0.0
        elif fixed_pts is not None:
            my_pct = my_hrs / total_hrs
            pts = round(fixed_pts * my_pct, 2)
        else:
            base = delivery_pts_by_hours(hrs_bracket)
            my_pct = my_hrs / total_hrs
            pts = round(base * my_pct, 2)

        has_ps = (
            "professional services" in proj_name.lower() or
            "professional services" in opp_name.lower()
        )

        proj_data = {
            "name": proj_name,
            "status": p.get("MPM4_BASE__Status__c", ""),
            "scoped_hours": fixed_pts if fixed_pts is not None else hrs_bracket,
            "scoped_hours_raw": scoped,  # None if not set in SF; used by JS for No PS recalc
            "scoped_label": "No hours logged" if my_hrs == 0 else scoped_label,
            "my_hours": my_hrs,
            "other_hours": round(other_hrs, 2),
            "other_names": other_names,
            "my_pct": round(my_pct * 100, 0),
            "my_pct_raw": round(my_pct, 6),
            "pts": pts,
            "no_hours": my_hrs == 0,
            "assisted": p["Id"] not in seen_proj_ids,
            "is_retainer": False,
            "has_ps": has_ps,
        }

        group_key = opp_id or p["Id"]  # group no-opp projects individually
        if group_key not in seen_opps:
            seen_opps[group_key] = len(delivery_by_opp)
            delivery_by_opp.append({
                "opp_name": opp_name,
                "opp_id": opp_id,
                "project_count": len(opp_proj_map[opp_id]),
                "projects": [proj_data],
            })
        else:
            delivery_by_opp[seen_opps[group_key]]["projects"].append(proj_data)

    # ── Retainer Delivery ─────────────────────────────────────────────────────
    # Retainer projects are awarded quarterly (not on completion).
    # Points = delivery_bracket(effective_hrs) * (my_hrs / total_hrs)
    # 75% rule: if total tracked >= 75% of (monthly * 3), use full quarterly hours;
    # otherwise use actual tracked hours for the bracket.
    retainer_projects = get_retainer_projects(sf, user_id, qs, qe)
    seen_retainer_ids = {p["Id"] for p in retainer_projects}

    for p in retainer_projects:
        proj_id   = p["Id"]
        proj_name = p.get("Name", "")
        opp_id    = p.get("MPM4_BASE__Opportunity__c")
        opp_name  = (p.get("MPM4_BASE__Opportunity__r") or {}).get("Name", "") or "(No Opportunity)"

        monthly_hrs  = p.get("Retainer_Monthly_Hours__c") or 0
        quarterly_hrs = monthly_hrs * 3

        my_hrs, other_hrs, other_names = get_logged_hours_in_quarter(sf, proj_id, user_id, qs, qe)
        total_hrs = my_hrs + other_hrs

        if total_hrs == 0 or monthly_hrs == 0:
            continue

        # 75% rule
        if total_hrs >= 0.75 * quarterly_hrs:
            effective_hrs = quarterly_hrs
            threshold_note = f"{monthly_hrs}h/mo × 3 = {quarterly_hrs}h (≥75% tracked)"
        else:
            effective_hrs = total_hrs
            threshold_note = f"{quarterly_hrs}h scoped, only {total_hrs:.1f}h tracked (<75%)"

        base_pts = delivery_pts_by_hours(effective_hrs)
        my_pct   = my_hrs / total_hrs
        pts      = round(base_pts * my_pct, 2)

        proj_data = {
            "name":             proj_name,
            "status":           p.get("MPM4_BASE__Status__c", ""),
            "scoped_hours":     quarterly_hrs,
            "scoped_hours_raw": None,
            "scoped_label":     threshold_note,
            "my_hours":         my_hrs,
            "other_hours":      round(other_hrs, 2),
            "other_names":      other_names,
            "my_pct":           round(my_pct * 100, 0),
            "my_pct_raw":       round(my_pct, 6),
            "pts":              pts,
            "no_hours":         False,
            "assisted":         False,
            "is_retainer":      True,
            "has_ps":           False,
        }

        group_key = opp_id or proj_id
        if group_key not in seen_opps:
            seen_opps[group_key] = len(delivery_by_opp)
            delivery_by_opp.append({
                "opp_name":     opp_name,
                "opp_id":       opp_id,
                "project_count": 1,
                "projects":     [proj_data],
            })
        else:
            delivery_by_opp[seen_opps[group_key]]["projects"].append(proj_data)

    # ── Active pipeline ───────────────────────────────────────────────────────
    active = sf.query_all(f"""
        SELECT Id, Name, MPM4_BASE__Status__c, MPM4_BASE__Opportunity__c,
               Scoped_Hours_PSS__c
        FROM MPM4_BASE__Milestone1_Project__c
        WHERE MPM4_BASE__Status__c NOT IN ('Completed', 'Terminated')
        AND MPM4_BASE__Opportunity__c IN (
            SELECT Id FROM Opportunity WHERE Sales_Engineer__c = '{user_id}'
        )
        ORDER BY LastModifiedDate DESC
        LIMIT 20
    """)["records"]

    act_opp_ids = list({p["MPM4_BASE__Opportunity__c"] for p in active if p.get("MPM4_BASE__Opportunity__c")})
    act_dt_map = {}
    if act_opp_ids:
        id_str = ",".join(f"'{i}'" for i in act_opp_ids)
        act_opps_raw = sf.query_all(f"SELECT Id, Name, Deal_Type__c FROM Opportunity WHERE Id IN ({id_str})")["records"]
        act_pf_map = get_product_family_map(sf, [o["Id"] for o in act_opps_raw])
        for o in act_opps_raw:
            act_dt_map[o["Id"]] = deal_type_for(o.get("Name", ""), o.get("Deal_Type__c"), act_pf_map.get(o["Id"]))

    pipeline = []
    for p in active:
        opp_id = p.get("MPM4_BASE__Opportunity__c")
        dt = act_dt_map.get(opp_id, "CPaaS")
        scoped = p.get("Scoped_Hours_PSS__c")
        hrs_bracket = scoped if scoped is not None else (4.0 if dt == "SaaS" else 2.0)
        fixed_pts = dc_to_dc_pts(p.get("Name", ""))
        my_hrs, other_hrs, other_names = get_logged_hours(sf, p["Id"], user_id)
        total_hrs = my_hrs + other_hrs
        if total_hrs > 0:
            base = fixed_pts if fixed_pts is not None else delivery_pts_by_hours(hrs_bracket)
            pts = round(base * (my_hrs / total_hrs), 2)
        else:
            pts = 0.0
        if pts > 0:
            pipeline.append({
                "name": p.get("Name", ""),
                "status": p.get("MPM4_BASE__Status__c", ""),
                "scoped_hours": hrs_bracket,
                "my_hours": my_hrs,
                "other_hours": round(other_hrs, 2),
                "other_names": other_names,
                "pts": pts,
            })

    # ── Pipeline Acquisition ──────────────────────────────────────────────────
    # Active opps (not Closed Won) where user is named SE — estimate acq pts if closed now
    active_opps = sf.query_all(f"""
        SELECT Id, Name, Account.Name, Type, Customer_Segment__c,
               Board__c, Opportunity_Owner_Board__c, Deal_Type__c, StageName
        FROM Opportunity
        WHERE Sales_Engineer__c = '{user_id}'
        AND StageName NOT IN ('Closed Won', 'Closed Lost')
        ORDER BY LastModifiedDate DESC
        LIMIT 30
    """)["records"]

    pipe_pf_map = get_product_family_map(sf, [o["Id"] for o in active_opps])
    pipeline_acq = []
    for o in active_opps:
        seg   = o.get("Customer_Segment__c") or ""
        otype = o.get("Type") or ""
        board = o.get("Opportunity_Owner_Board__c") or o.get("Board__c") or ""
        dt    = deal_type_for(o.get("Name", ""), o.get("Deal_Type__c"), pipe_pf_map.get(o["Id"]))
        base  = acq_pts(seg, otype, board, dt)
        if base > 0:
            pipeline_acq.append({
                "name":     o.get("Name", ""),
                "account":  (o.get("Account") or {}).get("Name", ""),
                "segment":  seg,
                "type":     otype,
                "board":    board,
                "deal_type": dt,
                "stage":    o.get("StageName", ""),
                "pts":      round(base, 2),
            })

    # ── Summary ───────────────────────────────────────────────────────────────
    acq_total = sum(r["pts"] for r in acquisition)
    del_total = sum(p["pts"] for opp in delivery_by_opp for p in opp["projects"])
    confirmed = min(acq_total + del_total, KPI_CAP)
    pipeline_del_pts = sum(r["pts"] for r in pipeline)
    pipeline_acq_pts = sum(r["pts"] for r in pipeline_acq)
    pipeline_pts = pipeline_del_pts + pipeline_acq_pts
    projected = min(confirmed + pipeline_pts, KPI_CAP)

    return {
        "quarter": f"Q{quarter} {year}",
        "acquisition": acquisition,
        "delivery": delivery_by_opp,
        "pipeline": pipeline,
        "pipeline_acq": pipeline_acq,
        "summary": {
            "acq_pts": round(acq_total, 2),
            "del_pts": round(del_total, 2),
            "confirmed": round(confirmed, 2),
            "pipeline_del_pts": round(pipeline_del_pts, 2),
            "pipeline_acq_pts": round(pipeline_acq_pts, 2),
            "pipeline_pts": round(pipeline_pts, 2),
            "projected": round(projected, 2),
            "target": KPI_TARGET,
            "cap": KPI_CAP,
        }
    }

# ── Auth Routes ────────────────────────────────────────────────────────────────

@app.route("/login")
def login():
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template_string(LOGIN_HTML, error=None)

@app.route("/auth")
def auth():
    """Perform SF CLI identity resolution and redirect to dashboard."""
    error = None
    try:
        resolve_identity()
        return redirect(url_for("index"))
    except Exception as e:
        error = str(e)
    return render_template_string(LOGIN_HTML, error=error)

@app.route("/logout")
def logout():
    session.pop("user_id", None)
    session.pop("user_name", None)
    session.pop("access_token", None)
    session.pop("instance_url", None)
    return redirect(url_for("login"))

# ── App Routes ─────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    resp = app.make_response(render_template_string(DASHBOARD_HTML,
        user_name=session.get("user_name", ""),
        user_id=session.get("user_id", "")
    ))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

@app.route("/api/kpi")
@login_required
def api_kpi():
    user_id = session["user_id"]
    year    = int(request.args.get("year",    datetime.now().year))
    quarter = int(request.args.get("quarter", (datetime.now().month - 1) // 3 + 1))
    try:
        sf = get_sf()
        data = calculate_kpi(sf, user_id, year, quarter)
        return jsonify(data)
    except Exception as e:
        err = str(e)
        tb = traceback.format_exc()
        if "INVALID_SESSION_ID" in err or "Session expired" in err:
            session.pop("user_id", None)
            return jsonify({"error": "Session expired. Please log in again.", "auth_required": True}), 401
        return jsonify({"error": err, "traceback": tb}), 500

# ── HTML Templates ─────────────────────────────────────────────────────────────

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SE KPI Dashboard — Login</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: linear-gradient(135deg, #0f3f6e 0%, #1a5c9e 100%);
    min-height: 100vh; display: flex; align-items: center; justify-content: center;
  }
  .card {
    background: white; border-radius: 16px; padding: 48px 40px;
    width: 100%; max-width: 420px; text-align: center;
    box-shadow: 0 20px 60px rgba(0,0,0,0.3);
  }
  .logo { font-size: 2.5rem; margin-bottom: 8px; }
  h1 { font-size: 1.5rem; font-weight: 700; color: #0f3f6e; }
  p { color: #64748b; font-size: 0.9rem; margin-top: 8px; margin-bottom: 32px; }
  .btn {
    display: inline-flex; align-items: center; gap: 10px;
    background: #0f3f6e; color: white; border: none; border-radius: 8px;
    padding: 14px 28px; font-size: 1rem; font-weight: 600; cursor: pointer;
    text-decoration: none; transition: background 0.2s; width: 100%; justify-content: center;
  }
  .btn:hover { background: #1a5c9e; }
  .error {
    background: #fef2f2; color: #dc2626; border: 1px solid #fecaca;
    border-radius: 8px; padding: 12px 16px; font-size: 0.82rem;
    margin-bottom: 20px; text-align: left; word-break: break-word;
  }
  .hint { margin-top: 20px; font-size: 0.78rem; color: #94a3b8; }
  code { background: #f1f5f9; padding: 2px 6px; border-radius: 4px; font-size: 0.8rem; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">📊</div>
  <h1>SE KPI Dashboard</h1>
  <p>Authenticating via your Salesforce CLI session&hellip;</p>
  {% if error %}
  <div class="error"><strong>Could not authenticate:</strong><br>{{ error }}</div>
  <p style="margin-bottom:16px;font-size:0.85rem;color:#64748b">
    Make sure you are logged in via the Salesforce CLI:
  </p>
  <a href="/auth" class="btn">Try Again</a>
  <p class="hint">Run <code>sf login -o &lt;your_alias&gt;</code> in your terminal if the error persists.</p>
  {% else %}
  <p style="color:#64748b;font-size:0.9rem;margin-bottom:28px">
    Authenticate using your Salesforce CLI session
  </p>
  <a href="/auth" class="btn">Login with Salesforce</a>
  {% endif %}
</div>
</body>
</html>"""


DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SE KPI Dashboard</title>
<style>
  :root {
    --primary: #0f3f6e;
    --primary-light: #1a5c9e;
    --accent: #00b4d8;
    --green: #22c55e;
    --yellow: #f59e0b;
    --red: #ef4444;
    --bg: #f1f5f9;
    --card: #ffffff;
    --border: #e2e8f0;
    --text: #1e293b;
    --muted: #64748b;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: var(--bg); color: var(--text); }

  header {
    background: linear-gradient(135deg, var(--primary) 0%, var(--primary-light) 100%);
    color: white; padding: 16px 32px;
    display: flex; align-items: center; justify-content: space-between;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
  }
  .header-left h1 { font-size: 1.4rem; font-weight: 700; }
  .header-left p { font-size: 0.82rem; opacity: 0.75; margin-top: 2px; }
  .user-info { display: flex; align-items: center; gap: 14px; }
  .user-chip {
    background: rgba(255,255,255,0.15); border-radius: 99px;
    padding: 6px 16px; font-size: 0.875rem; font-weight: 600;
    display: flex; align-items: center; gap: 6px;
  }
  .user-chip svg { width: 16px; height: 16px; opacity: 0.8; }
  .logout-btn {
    background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.25);
    color: white; border-radius: 8px; padding: 6px 14px; font-size: 0.8rem;
    cursor: pointer; text-decoration: none; transition: background 0.2s;
  }
  .logout-btn:hover { background: rgba(255,255,255,0.2); }

  .controls {
    background: var(--card); border-bottom: 1px solid var(--border);
    padding: 14px 32px; display: flex; gap: 16px; align-items: flex-end; flex-wrap: wrap;
  }
  .control-group { display: flex; flex-direction: column; gap: 4px; }
  .control-group label { font-size: 0.72rem; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }
  select, button {
    padding: 8px 14px; border: 1px solid var(--border); border-radius: 8px;
    font-size: 0.9rem; outline: none; cursor: pointer;
  }
  select { background: white; color: var(--text); }
  select:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(0,180,216,0.15); }
  button {
    background: var(--primary); color: white; border: none; font-weight: 600;
    padding: 9px 22px; transition: background 0.2s;
  }
  button:hover { background: var(--primary-light); }
  button:disabled { background: var(--muted); cursor: not-allowed; }

  main { max-width: 1300px; margin: 0 auto; padding: 28px 24px; display: flex; flex-direction: column; gap: 24px; }

  .summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; }
  .stat-card {
    background: var(--card); border-radius: 12px; padding: 20px 24px;
    border: 1px solid var(--border); box-shadow: 0 1px 4px rgba(0,0,0,0.04);
  }
  .stat-card .label { font-size: 0.75rem; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
  .stat-card .value { font-size: 2rem; font-weight: 700; margin-top: 4px; }
  .stat-card .sub { font-size: 0.8rem; color: var(--muted); margin-top: 2px; }
  .stat-card.green .value { color: var(--green); }
  .stat-card.yellow .value { color: var(--yellow); }
  .stat-card.red .value { color: var(--red); }

  .progress-card {
    background: var(--card); border-radius: 12px; padding: 24px;
    border: 1px solid var(--border); box-shadow: 0 1px 4px rgba(0,0,0,0.04);
  }
  .progress-card h3 { font-size: 0.85rem; color: var(--muted); font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 12px; }
  .progress-bar-wrap { background: #e2e8f0; border-radius: 99px; height: 14px; overflow: hidden; }
  .progress-bar { height: 100%; border-radius: 99px; transition: width 0.6s ease; }
  .progress-bar.green { background: linear-gradient(90deg, #22c55e, #16a34a); }
  .progress-bar.yellow { background: linear-gradient(90deg, #f59e0b, #d97706); }
  .progress-bar.red { background: linear-gradient(90deg, #ef4444, #dc2626); }
  .progress-bar.projected { background: rgba(0,180,216,0.35); }
  .progress-labels { display: flex; justify-content: space-between; margin-top: 6px; font-size: 0.78rem; color: var(--muted); }

  .section-card {
    background: var(--card); border-radius: 12px; border: 1px solid var(--border);
    box-shadow: 0 1px 4px rgba(0,0,0,0.04);
  }
  .section-card > table, .section-card > .section-header { overflow: hidden; }
  .section-header {
    padding: 16px 24px; border-bottom: 1px solid var(--border);
    display: flex; justify-content: space-between; align-items: center;
  }
  .section-header h2 { font-size: 1rem; font-weight: 700; }
  .section-header .total-badge {
    background: var(--primary); color: white; border-radius: 99px;
    padding: 3px 14px; font-size: 0.85rem; font-weight: 700;
  }

  table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
  th { padding: 10px 16px; text-align: left; font-size: 0.72rem; font-weight: 700;
       text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted);
       background: #f8fafc; border-bottom: 1px solid var(--border); }
  td { padding: 11px 16px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: #f8fafc; }

  .opp-row td { background: #f8fafc; font-weight: 700; font-size: 0.82rem; color: var(--primary); padding: 8px 16px; }
  .proj-row td:first-child { padding-left: 32px; }

  .badge {
    display: inline-flex; align-items: center; justify-content: center;
    padding: 2px 10px; border-radius: 99px; font-size: 0.75rem; font-weight: 700;
  }
  .badge-A    { background: #dbeafe; color: #1d4ed8; }
  .badge-Ar   { background: #ede9fe; color: #6d28d9; }
  .badge-B    { background: #dcfce7; color: #15803d; }
  .badge-C    { background: #fef9c3; color: #854d0e; }
  .badge-D    { background: #f1f5f9; color: var(--muted); }
  .badge-info { background: #f0f9ff; color: #0369a1; }
  .shared-tag { display: inline-block; font-size: 0.7rem; color: var(--yellow); background: #fefce8; border: 1px solid #fde68a; border-radius: 4px; padding: 1px 6px; margin-left: 6px; }
  .accel-btn {
    padding: 2px 7px; font-size: 0.7rem; font-weight: 700; border-radius: 4px; cursor: pointer;
    background: #f1f5f9; border: 1px solid #e2e8f0; color: var(--muted);
    transition: background 0.15s, color 0.15s;
  }
  .accel-btn.active { background: #fef9c3; border-color: #f59e0b; color: #92400e; }
  .multi-proj-tag { display: inline-block; font-size: 0.7rem; color: var(--accent); background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 4px; padding: 1px 6px; margin-left: 6px; }
  .pts-cell { font-weight: 700; color: var(--primary); font-size: 0.95rem; }
  .pts-cell.shared { color: var(--yellow); }

  .no-data { padding: 40px; text-align: center; color: var(--muted); }
  .spinner {
    display: none; margin: 60px auto; width: 44px; height: 44px;
    border: 4px solid var(--border); border-top-color: var(--primary);
    border-radius: 50%; animation: spin 0.8s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  #error-msg { background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; border-radius: 8px; padding: 14px 20px; display: none; }
  #dashboard { display: none; }
  #placeholder { text-align: center; padding: 80px 20px; color: var(--muted); }
  #placeholder svg { width: 64px; height: 64px; opacity: 0.3; margin-bottom: 16px; }
  #placeholder p { font-size: 1rem; }
</style>
</head>
<body>

<header>
  <div class="header-left">
    <h1>SE KPI Dashboard</h1>
    <p>Solution Engineer Incentive Points Estimator</p>
  </div>
  <div class="user-info">
    <div class="user-chip">
      <svg viewBox="0 0 24 24" fill="currentColor"><path d="M12 12c2.7 0 4.8-2.1 4.8-4.8S14.7 2.4 12 2.4 7.2 4.5 7.2 7.2 9.3 12 12 12zm0 2.4c-3.2 0-9.6 1.6-9.6 4.8v2.4h19.2v-2.4c0-3.2-6.4-4.8-9.6-4.8z"/></svg>
      {{ user_name }}
    </div>
    <a href="/logout" class="logout-btn">Log out</a>
  </div>
</header>

<div class="controls">
  <div class="control-group">
    <label>Quarter</label>
    <select id="quarter-select">
      <option value="1">Q1</option>
      <option value="2">Q2</option>
      <option value="3">Q3</option>
      <option value="4">Q4</option>
    </select>
  </div>
  <div class="control-group">
    <label>Year</label>
    <select id="year-select"></select>
  </div>
  <div class="control-group" style="margin-left:8px">
    <label>&nbsp;</label>
    <button id="calc-btn" onclick="calculate()">Calculate KPI</button>
  </div>
</div>

<main>
  <div id="placeholder">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
      <path stroke-linecap="round" stroke-linejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z"/>
    </svg>
    <p>Select a quarter and click Calculate KPI</p>
  </div>
  <div class="spinner" id="spinner"></div>
  <div id="error-msg"></div>
  <div id="dashboard">

    <div class="summary-grid" id="stat-cards"></div>
    <div class="progress-card" id="progress-card"></div>

    <div class="section-card">
      <div class="section-header">
        <h2>Acquisition Points <span style="color:var(--muted);font-weight:400;font-size:0.85rem">Closed Won this quarter</span></h2>
        <span class="total-badge" id="acq-total-badge">0 pts</span>
      </div>
      <table>
        <thead><tr>
          <th>Opportunity</th><th>Account</th><th>Segment</th>
          <th>Type</th><th>Board</th><th>Deal</th>
          <th style="text-align:right">My SS Hrs</th><th style="text-align:right">Other SS</th><th style="text-align:right">My %</th>
          <th style="text-align:right">Points</th><th style="text-align:center">Accel.</th><th>Notes</th>
        </tr></thead>
        <tbody id="acq-body"></tbody>
      </table>
    </div>

    <div class="section-card">
      <div class="section-header">
        <h2>Delivery Points <span style="color:var(--muted);font-weight:400;font-size:0.85rem">Completed projects this quarter</span></h2>
        <span class="total-badge" id="del-total-badge">0 pts</span>
      </div>
      <table>
        <thead><tr>
          <th>Opportunity / Project</th><th style="text-align:right">Scoped</th>
          <th style="text-align:right">My Hrs</th><th style="text-align:right">Other Hrs</th>
          <th style="text-align:right">My %</th><th style="text-align:right">Points</th>
          <th>PS Type</th><th>Notes</th>
        </tr></thead>
        <tbody id="del-body"></tbody>
      </table>
    </div>

    <div class="section-card" id="pipeline-section">
      <div class="section-header">
        <h2>Pipeline <span style="color:var(--muted);font-weight:400;font-size:0.85rem">Estimated if closed/completed now</span></h2>
        <span class="total-badge" id="pipe-total-badge">0 pts</span>
      </div>
      <p style="font-size:0.8rem;color:var(--muted);margin:0 0 8px 0">Delivery <span id="pipe-del-badge" style="font-weight:600;color:var(--text)">0 pts</span></p>
      <table>
        <thead><tr>
          <th>Project</th><th>Status</th><th style="text-align:right">Scoped</th>
          <th style="text-align:right">My Hrs</th><th style="text-align:right">Other Hrs</th>
          <th style="text-align:right">Est. Points</th>
        </tr></thead>
        <tbody id="pipe-body"></tbody>
      </table>
      <p style="font-size:0.8rem;color:var(--muted);margin:16px 0 8px 0">Acquisition <span id="pipe-acq-badge" style="font-weight:600;color:var(--text)">0 pts</span></p>
      <table>
        <thead><tr>
          <th>Opportunity</th><th>Segment</th><th>Type</th><th>Deal</th><th>Stage</th>
          <th style="text-align:right">Est. Points</th>
        </tr></thead>
        <tbody id="pipe-acq-body"></tbody>
      </table>
    </div>

  </div>
</main>

<script>
const now = new Date();
const curQ = Math.floor(now.getMonth() / 3) + 1;
const curY = now.getFullYear();

const yearSel = document.getElementById('year-select');
for (let y = curY; y >= curY - 2; y--) {
  const opt = document.createElement('option');
  opt.value = y; opt.text = y;
  yearSel.appendChild(opt);
}
document.getElementById('quarter-select').value = curQ;

function fmt(n) {
  if (n === undefined || n === null) return '-';
  const v = parseFloat(n);
  if (isNaN(v)) return '-';
  return v % 1 === 0 ? v.toString() : v.toFixed(2);
}

function badge(seg) {
  if (!seg) return '<span class="badge badge-D">?</span>';
  return `<span class="badge badge-${seg}">${seg}</span>`;
}

function calculate() {
  const q = document.getElementById('quarter-select').value;
  const y = document.getElementById('year-select').value;

  document.getElementById('placeholder').style.display = 'none';
  document.getElementById('dashboard').style.display = 'none';
  document.getElementById('error-msg').style.display = 'none';
  document.getElementById('spinner').style.display = 'block';
  document.getElementById('calc-btn').disabled = true;

  fetch(`/api/kpi?year=${y}&quarter=${q}`)
    .then(r => {
      if (r.status === 401) { window.location.href = '/login'; return; }
      return r.json();
    })
    .then(data => {
      if (!data) return;
      document.getElementById('spinner').style.display = 'none';
      document.getElementById('calc-btn').disabled = false;
      if (data.error) { showError(data.error); return; }
      const isCurrent = parseInt(q) === curQ && parseInt(y) === curY;
      renderDashboard(data, isCurrent);
    })
    .catch(e => {
      document.getElementById('spinner').style.display = 'none';
      document.getElementById('calc-btn').disabled = false;
      if (e.message === 'session_expired') {
        window.location.href = '/login';
      } else {
        showError(e.toString());
      }
    });
}

function showError(msg) {
  const el = document.getElementById('error-msg');
  el.textContent = 'Error: ' + msg;
  el.style.display = 'block';
}

const PS_PTS = {"2":2,"2t":2,"5":5,"7.5":7.5,"10s":10,"10g":10,"15":15,"5d":5,"10d":10,"15u":15};
const PS_ADDITIVE = new Set(["2","2t"]); // Tune-Up and Training add CPaaS base (2pts)
const CPAAS_BASE_PTS = 2;

function deliveryPtsByHours(hrs) {
  if (hrs < 15)  return 2;
  if (hrs < 35)  return 5;
  if (hrs < 75)  return 10;
  if (hrs < 100) return 15;
  if (hrs < 150) return 20;
  if (hrs < 350) return 50;
  return 100;
}

function onPsSelect(sel) {
  const row = sel.closest('tr');
  const ptsCell = row.querySelector('.proj-pts-cell');
  const basePts = parseFloat(sel.dataset.basePts);
  const myPct = parseFloat(sel.dataset.myPct);
  const scopedRaw = sel.dataset.scopedRaw;

  if (!sel.value) {
    ptsCell.textContent = fmt(basePts);
  } else if (sel.value === 'zero') {
    ptsCell.textContent = fmt(0);
  } else if (sel.value === 'nops_cpaas' || sel.value === 'nops_saas') {
    const fallback = sel.value === 'nops_saas' ? 4.0 : 2.0;
    const hrs = scopedRaw !== '' ? parseFloat(scopedRaw) : fallback;
    const fullPts = deliveryPtsByHours(hrs);
    ptsCell.textContent = fmt(Math.round(fullPts * myPct * 100) / 100);
  } else {
    const psPts = PS_PTS[sel.value] || 0;
    const fullPts = PS_ADDITIVE.has(sel.value) ? CPAAS_BASE_PTS + psPts : psPts;
    ptsCell.textContent = fmt(Math.round(fullPts * myPct * 100) / 100);
  }
  updateDelTotal();
}

function updateDelTotal() {
  const s = window._kpiSummary;
  if (!s) return;
  let delTotal = 0;
  document.querySelectorAll('.proj-pts-cell').forEach(cell => {
    delTotal += parseFloat(cell.textContent) || 0;
  });
  delTotal = Math.round(delTotal * 100) / 100;
  const acqTotal = parseFloat(document.getElementById('acq-pts-value').textContent) || s.acq_pts;
  const confirmed = Math.min(acqTotal + delTotal, s.cap);
  const color = confirmed >= s.target ? 'green' : confirmed >= s.target * 0.75 ? 'yellow' : 'red';
  document.getElementById('del-total-badge').textContent = fmt(delTotal) + ' pts';
  document.getElementById('del-pts-value').textContent = fmt(delTotal);
  document.getElementById('confirmed-value').textContent = fmt(confirmed);
  document.getElementById('confirmed-sub').textContent = `of ${s.target} target (${Math.round(confirmed/s.target*100)}%)`;
  document.getElementById('confirmed-card').className = `stat-card ${color}`;
}

function toggleAccel(btn) {
  const row = btn.closest('tr');
  const basePts = parseFloat(row.dataset.basePts);
  const isOn = btn.classList.contains('active');
  const ptsCell = row.querySelector('.pts-cell');
  if (isOn) {
    btn.classList.remove('active');
    btn.title = 'Apply 1.5x Named Account accelerator';
    ptsCell.textContent = fmt(basePts);
  } else {
    btn.classList.add('active');
    btn.title = 'Remove 1.5x accelerator';
    ptsCell.textContent = fmt(Math.round(basePts * 1.5 * 100) / 100);
  }
  updateAcqTotal();
}

function updateAcqTotal() {
  const s = window._kpiSummary;
  if (!s) return;
  let acqTotal = 0;
  document.querySelectorAll('#acq-body tr[data-base-pts]').forEach(row => {
    const basePts = parseFloat(row.dataset.basePts);
    const isAccel = row.querySelector('.accel-btn.active') !== null;
    acqTotal += isAccel ? Math.round(basePts * 1.5 * 100) / 100 : basePts;
  });
  acqTotal = Math.round(acqTotal * 100) / 100;
  const delTotal = parseFloat(document.getElementById('del-pts-value').textContent) || s.del_pts;
  const confirmed = Math.min(acqTotal + delTotal, s.cap);
  const projected = Math.min(confirmed + s.pipeline_pts, s.cap);
  const color = confirmed >= s.target ? 'green' : confirmed >= s.target * 0.75 ? 'yellow' : 'red';
  document.getElementById('acq-total-badge').textContent = fmt(acqTotal) + ' pts';
  document.getElementById('acq-pts-value').textContent = fmt(acqTotal);
  document.getElementById('confirmed-value').textContent = fmt(confirmed);
  document.getElementById('confirmed-sub').textContent = `of ${s.target} target (${Math.round(confirmed/s.target*100)}%)`;
  document.getElementById('confirmed-card').className = `stat-card ${color}`;
}

function renderDashboard(data, isCurrent) {
  window._kpiSummary = data.summary;
  const s = data.summary;
  const pct = Math.min((s.confirmed / s.target) * 100, 100);
  const projPct = Math.min((s.projected / s.target) * 100, 100);
  const color = s.confirmed >= s.target ? 'green' : s.confirmed >= s.target * 0.75 ? 'yellow' : 'red';

  document.getElementById('stat-cards').innerHTML = `
    <div class="stat-card ${color}" id="confirmed-card">
      <div class="label">Confirmed Total</div>
      <div class="value" id="confirmed-value">${fmt(s.confirmed)}</div>
      <div class="sub" id="confirmed-sub">of ${s.target} target (${Math.round(s.confirmed/s.target*100)}%)</div>
    </div>
    <div class="stat-card">
      <div class="label">Acquisition</div>
      <div class="value" id="acq-pts-value" style="color:var(--primary)">${fmt(s.acq_pts)}</div>
      <div class="sub">pts from closed won</div>
    </div>
    <div class="stat-card">
      <div class="label">Delivery</div>
      <div class="value" id="del-pts-value" style="color:var(--primary)">${fmt(s.del_pts)}</div>
      <div class="sub">pts from completed projects</div>
    </div>
    ${isCurrent ? `
    <div class="stat-card" style="border-color:#bae6fd">
      <div class="label">Projected (+ pipeline)</div>
      <div class="value" style="color:var(--accent)">${fmt(s.projected)}</div>
      <div class="sub">if all active projects close</div>
    </div>` : ''}
  `;

  document.getElementById('progress-card').innerHTML = `
    <h3>${data.quarter} Progress toward ${s.target} pt target</h3>
    <div class="progress-bar-wrap" style="margin-bottom:6px">
      <div class="progress-bar ${color}" style="width:${pct}%"></div>
    </div>
    ${isCurrent && s.projected > s.confirmed ? `
    <div class="progress-bar-wrap">
      <div class="progress-bar projected" style="width:${projPct}%"></div>
    </div>` : ''}
    <div class="progress-labels">
      <span>0</span><span>${s.target} (target)</span><span>${s.cap} (CAP)</span>
    </div>
    ${s.confirmed < s.target
      ? `<p style="margin-top:10px;font-size:0.85rem;color:var(--muted)">Need <strong>${fmt(s.target - s.confirmed)}</strong> more pts to hit target.${isCurrent ? ` Projected <strong>${fmt(s.projected)}</strong> if pipeline closes.` : ''}</p>`
      : `<p style="margin-top:10px;font-size:0.85rem;color:var(--green)"><strong>Target reached!</strong> Confirmed ${fmt(s.confirmed)} pts.</p>`}
  `;

  document.getElementById('acq-total-badge').textContent = fmt(s.acq_pts) + ' pts';
  const acqBody = document.getElementById('acq-body');
  if (!data.acquisition.length) {
    acqBody.innerHTML = '<tr><td colspan="12" class="no-data">No closed won opportunities this quarter</td></tr>';
  } else {
    acqBody.innerHTML = data.acquisition.map(r => {
      const assistedTag = r.assisted ? `<span class="multi-proj-tag">Assisted</span>` : '';
      const ssOther = r.other_ss_hrs > 0
        ? `<span style="color:var(--yellow)">${fmt(r.other_ss_hrs)}h (${r.other_ss_names.join(', ')})</span>`
        : `<span style="color:var(--muted)">-</span>`;
      const pctStr = r.other_ss_hrs > 0 ? `${r.my_ss_pct}%` : (r.my_ss_hrs > 0 ? '100%' : '-');
      return `<tr data-base-pts="${r.pts}" ${r.my_ss_hrs === 0 ? 'style="opacity:0.55"' : ''}>
        <td style="max-width:220px">${r.name}${assistedTag}</td>
        <td style="color:var(--muted)">${r.account}</td>
        <td>${badge(r.segment)}</td>
        <td><span class="badge badge-info">${r.type || '?'}</span></td>
        <td style="font-size:0.8rem;color:var(--muted)">${r.board}</td>
        <td><span class="badge ${r.deal_type==='SaaS'?'badge-Ar':'badge-B'}">${r.deal_type}</span></td>
        <td style="text-align:right">${r.my_ss_hrs > 0 ? fmt(r.my_ss_hrs)+'h' : '<span style="color:var(--muted)">-</span>'}</td>
        <td style="text-align:right">${ssOther}</td>
        <td style="text-align:right">${pctStr}</td>
        <td style="text-align:right" class="pts-cell ${r.other_ss_hrs > 0 ? 'shared' : ''}">${fmt(r.pts)}</td>
        <td style="text-align:center"><button class="accel-btn" onclick="toggleAccel(this)" title="Apply 1.5x Named Account accelerator">1.5x</button></td>
        <td style="font-size:0.78rem;color:${r.notes.includes('No SS&P')?'var(--red,#ef4444)':'var(--muted)'}">${r.notes}</td>
      </tr>`;
    }).join('');
  }

  document.getElementById('del-total-badge').textContent = fmt(s.del_pts) + ' pts';
  const delBody = document.getElementById('del-body');
  if (!data.delivery.length) {
    delBody.innerHTML = '<tr><td colspan="8" class="no-data">No completed projects this quarter</td></tr>';
  } else {
    let html = '';
    data.delivery.forEach(opp => {
      const multiTag = opp.project_count > 1 ? `<span class="multi-proj-tag">${opp.project_count} projects</span>` : '';
      html += `<tr class="opp-row"><td colspan="8">${opp.opp_name}${multiTag}</td></tr>`;
      opp.projects.forEach(p => {
        const sharedTag = p.other_hours > 0 ? `<span class="shared-tag">Shared</span>` : '';
        const assistedTag = p.assisted ? `<span class="multi-proj-tag">Assisted</span>` : '';
        const retainerTag = p.is_retainer ? `<span class="multi-proj-tag" style="color:#7c3aed;background:#ede9fe;border-color:#ddd6fe">Retainer</span>` : '';
        const pctStr = p.other_hours > 0 ? `${p.my_pct}%` : '100%';
        const othersStr = p.other_hours > 0 ? `${fmt(p.other_hours)}h (${p.other_names.join(', ')})` : '-';
        const psDropdown = `
          <select class="ps-select" data-base-pts="${p.pts}" data-my-pct="${p.my_pct_raw}" data-scoped-raw="${p.scoped_hours_raw ?? ''}" onchange="onPsSelect(this)" style="font-size:0.75rem;padding:2px 4px;border:1px solid var(--border);border-radius:4px;background:white;color:var(--text);max-width:175px">
            <option value="">Auto</option>
            <option value="zero">Exclude (0 pts)</option>
            <option value="nops_cpaas">No PS (CPaaS)</option>
            <option value="nops_saas">No PS (SaaS)</option>
            <option value="2">Tune-Up (CPaaS + 8h · 4pts)</option>
            <option value="2t">Training (CPaaS + 14h · 4pts)</option>
            <option value="5">Guided Launch (16h · 5pts)</option>
            <option value="7.5">Email Premium Launch (24h · 7.5pts)</option>
            <option value="10s">Configured Start (35h · 10pts)</option>
            <option value="10g">Configured Grow (65h · 10pts)</option>
            <option value="15">Configured Scale (85h · 15pts)</option>
            <option value="5d">CX Discovery (20h · 5pts)</option>
            <option value="10d">CX Design (40h · 10pts)</option>
            <option value="15u">CX Uplift (80h · 15pts)</option>
          </select>`;
        html += `<tr class="proj-row" ${p.no_hours ? 'style="opacity:0.55"' : ''}>
          <td>${p.name}${sharedTag}${assistedTag}${retainerTag}</td>
          <td style="text-align:right">${fmt(p.scoped_hours)}h</td>
          <td style="text-align:right">${fmt(p.my_hours)}h</td>
          <td style="text-align:right;color:${p.other_hours>0?'var(--yellow)':'var(--muted)'}">${othersStr}</td>
          <td style="text-align:right">${pctStr}</td>
          <td style="text-align:right" class="pts-cell proj-pts-cell ${p.other_hours>0?'shared':''}">${fmt(p.pts)}</td>
          <td>${psDropdown}</td>
          <td style="font-size:0.78rem;color:${p.no_hours?'var(--red,#ef4444)':'var(--muted)'}">${p.scoped_label}</td>
        </tr>`;
      });
    });
    delBody.innerHTML = html;
  }

  document.getElementById('pipeline-section').style.display = isCurrent ? '' : 'none';
  if (isCurrent) {
    document.getElementById('pipe-total-badge').textContent = fmt(s.pipeline_pts) + ' pts';
    document.getElementById('pipe-del-badge').textContent = fmt(s.pipeline_del_pts) + ' pts';
    document.getElementById('pipe-acq-badge').textContent = fmt(s.pipeline_acq_pts) + ' pts';

    const pipeBody = document.getElementById('pipe-body');
    if (!data.pipeline.length) {
      pipeBody.innerHTML = '<tr><td colspan="6" class="no-data">No active projects with logged hours</td></tr>';
    } else {
      pipeBody.innerHTML = data.pipeline.map(r => `
        <tr>
          <td>${r.name}</td>
          <td><span class="badge badge-info">${r.status}</span></td>
          <td style="text-align:right">${fmt(r.scoped_hours)}h</td>
          <td style="text-align:right">${fmt(r.my_hours)}h</td>
          <td style="text-align:right;color:${r.other_hours>0?'var(--yellow)':'var(--muted)'}">${r.other_hours>0 ? fmt(r.other_hours)+'h' : '-'}</td>
          <td style="text-align:right" class="pts-cell">${fmt(r.pts)}</td>
        </tr>
      `).join('');
    }

    const pipeAcqBody = document.getElementById('pipe-acq-body');
    if (!data.pipeline_acq.length) {
      pipeAcqBody.innerHTML = '<tr><td colspan="6" class="no-data">No active opportunities</td></tr>';
    } else {
      pipeAcqBody.innerHTML = data.pipeline_acq.map(r => `
        <tr>
          <td style="max-width:220px">${r.name}</td>
          <td>${badge(r.segment)}</td>
          <td><span class="badge badge-info">${r.type || '?'}</span></td>
          <td><span class="badge ${r.deal_type==='SaaS'?'badge-Ar':'badge-B'}">${r.deal_type}</span></td>
          <td style="font-size:0.78rem;color:var(--muted)">${r.stage}</td>
          <td style="text-align:right" class="pts-cell">${fmt(r.pts)}</td>
        </tr>
      `).join('');
    }
  }

  document.getElementById('dashboard').style.display = 'block';
}
</script>
</body>
</html>"""

if __name__ == "__main__":
    print("Starting KPI Dashboard at http://localhost:5000")
    app.run(debug=False, port=5000)
