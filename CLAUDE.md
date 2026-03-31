# CLAUDE.md — SE KPI Estimation Project

This file describes the codebase for another Claude instance to understand, extend, or debug this project.

---

## What This Project Does

Calculates KPI incentive points for Infobip Solution Engineers. There are two tools:
- `kpi_server.py` — Flask web dashboard at `http://localhost:5000`
- `kpi_estimate.py` — Quick CLI printout in the terminal

Both connect to Salesforce via the Salesforce CLI (`sf` command). No OAuth app or Connected App is needed.

---

## Setup Requirements

### 1. Salesforce CLI authentication
```
sf login -o infobip
```
All SF queries use the token from this session. If you get auth errors, re-run the login.

### 2. config.py (gitignored — never commit)
Create `config.py` from the template:
```python
SF_ALIAS = "infobip"   # the alias used with sf login -o <alias>
```

### 3. Python dependencies
```
pip install flask simple_salesforce requests rich
```

### 4. Run
```
python kpi_server.py       # web dashboard
python kpi_estimate.py     # CLI
```

---

## File Structure

```
kpi_server.py        Main Flask app — web dashboard with auth, API, and HTML
kpi_estimate.py      CLI-only KPI estimator (no auth, hardcodes current quarter)
config.py            Local only — SF_ALIAS = "infobip" (gitignored)
config.example.py    Committed template for config.py
CLAUDE.md            This file
.gitignore           Excludes config.py, __pycache__, etc.
```

---

## Authentication (kpi_server.py)

Uses SF CLI — no Connected App needed. Flow:

1. User visits `/` → `@login_required` redirects to `/login`
2. `/login` renders the login page (does NOT auto-authenticate)
3. User clicks "Login with Salesforce" → hits `/auth`
4. `/auth` calls `resolve_identity()`:
   - Runs `sf org display -o <SF_ALIAS> --json` → gets `accessToken` + `instanceUrl`
   - Calls `GET {instanceUrl}/services/oauth2/userinfo` with Bearer token → gets `user_id` and `display_name`
   - Stores both in Flask session
5. Subsequent requests use session `user_id` and `access_token`

**Logout**: `/logout` pops all session keys individually (do NOT use `session.clear()` — it does not reliably mark the Flask session as modified, causing the logout to silently fail).

```python
# CORRECT logout pattern:
session.pop("user_id", None)
session.pop("user_name", None)
session.pop("access_token", None)
session.pop("instance_url", None)
```

---

## KPI Scoring Rules

### Acquisition Points

Awarded when an Opportunity reaches `Closed Won`. Base points by customer segment:

| Segment | Points |
|---------|--------|
| A       | 10     |
| Ar      | 8      |
| B       | 5      |
| C       | 2      |
| D       | 0      |

**Upsell decelerators** — applied only for `Type = 'Upsell'`, per board:

| Board             | CPaaS | SaaS |
|-------------------|-------|------|
| Africa            | 0.4   | 0.8  |
| Asia and Pacific  | 0.2   | 0.6  |
| Eurasia           | 1.0   | 1.0  |
| Europe            | 0.3   | 0.5  |
| India             | 0.2   | 0.6  |
| LatAm             | 0.2   | 0.6  |
| MENA              | 0.5   | 0.7  |
| North America     | 0.5   | 0.5  |
| Global            | 1.0   | 1.0  |

Deal type rule: if opportunity name contains "Professional Services" → treat as SaaS. Otherwise use `Deal_Type__c`.

**Shared acquisition** — points are split proportionally among all SEs who logged time on the **"Service Sales and Planning"** milestone tasks:

```
user_pts = base_pts × (my_ss_hours / total_ss_hours)
```

If no Service Sales & Planning hours are found at all, full points go to the named SE.

All SEs who logged SS&P time on an opportunity appear in the acquisition table, even if they are not the named `Sales_Engineer__c` on the opportunity (called "Assisted").

### Delivery Points

Awarded when a project reaches `Completed` status and the linked Opportunity is `Closed Won`. Points are based on scoped hours:

| Scoped Hours    | Points |
|-----------------|--------|
| 0 – 14h         | 2      |
| 15 – 34h        | 5      |
| 35 – 74h        | 10     |
| 75 – 99h        | 15     |
| 100 – 149h      | 20     |
| 150 – 349h      | 50     |
| 350h+           | 100    |

**Scoped hours source**: `Scoped_Hours_PSS__c` on the project. If null: SaaS fallback = 4h, CPaaS fallback = 2h.

**PS Type dropdown (manual override)**: For projects where the opportunity or project name contains "Professional Services", the dashboard shows a **PS Type** dropdown on the delivery row. The SE should select the correct PS package to get an accurate point estimate — the auto fallback is often imprecise.

Scoring rules for each PS type:

| PS Type | Logic | Result |
|---|---|---|
| Tune-Up (8h) | CPaaS base (2pts) + PS (2pts) | **4 pts** |
| Training (14h) | CPaaS base (2pts) + PS (2pts) | **4 pts** |
| Guided Launch (16h) | PS only | **5 pts** |
| Email Premium Launch (24h) | PS only | **7.5 pts** |
| Configured Start (35h) | PS only | **10 pts** |
| Configured Grow (65h) | PS only | **10 pts** |
| Configured Scale (85h) | PS only | **15 pts** |
| CX Discovery (20h) | PS only | **5 pts** |
| CX Design (40h) | PS only | **10 pts** |
| CX Uplift (80h) | PS only | **15 pts** |

Tune-Up and Training are **additive** (CPaaS component is always included) because the official KPI rule states CPaaS + Training is calculated separately, not as combined sold hours. All other PS packages use only the PS package points.

Points are always proportionally split by logged hours: `pts = full_pts × (my_hrs / total_hrs)`.

> **Note for users**: The dashboard cannot automatically detect the PS package type from Salesforce. Always set the PS Type dropdown on any "Professional Services" project to get a correct delivery point estimate.

**Shared delivery**:
```
user_pts = base_pts × (my_logged_hours / total_logged_hours)
```
All SEs' hours on all tasks of the project are counted. SE who logged time but is not the named SE on the opportunity also gets credit ("Assisted" projects).

**Special project types:**

DC to DC migrations (project name contains `[DC to DC]`):
- Fixed points regardless of scoped hours
- `[DC to DC SaaS]` = 7 pts
- `[DC to DC CPaaS]` = 4 pts
- Points split proportionally by logged hours

Retainer projects (project name contains `[Retainer]`):
- Awarded every quarter while project is active — NOT only on completion
- SF field: `Retainer_Monthly_Hours__c` on the project
- Quarterly hours = `Retainer_Monthly_Hours__c × 3`
- **75% rule**: if total tracked hours in quarter ≥ 75% of quarterly hours → use full quarterly hours for bracket; otherwise use actual tracked hours
- Points split proportionally: `user_pts = base_pts × (my_hrs_in_quarter / total_hrs_in_quarter)`
- Time entries are filtered to the quarter (not all-time) for retainer projects

### Named Account Accelerator

For Named Accounts, total acquired points are multiplied by **1.5**. This is the only accelerator rate.

Named accounts are maintained by Customer Operations and updated twice a year. There is no Salesforce field that reliably identifies named accounts programmatically — the dashboard provides a manual **1.5x button** on each acquisition row so the SE can apply the accelerator per opp as needed.

Target: **71 pts/quarter**. CAP: **130 pts**.

---

## Salesforce Data Model

### Objects Used

**Opportunity**
- `Sales_Engineer__c` — User lookup (the SE assigned to this opp)
- `Customer_Segment__c` — A / Ar / B / C / D
- `Deal_Type__c` — SaaS / CPaaS
- `Type` — New Business / Upsell / etc.
- `Opportunity_Owner_Board__c` — e.g. "Board Asia and Pacific"
- `Board__c` — fallback board field
- `StageName` — filter to 'Closed Won'
- `CloseDate` — used for quarter filtering

**MPM4_BASE__Milestone1_Project__c** (projects)
- `MPM4_BASE__Opportunity__c` — linked Opportunity
- `MPM4_BASE__Status__c` — Active / Completed / Terminated
- `Date_Completed__c` — use this for quarter filtering (NOT `LastModifiedDate`)
- `Scoped_Hours_PSS__c` — sold/scoped hours for PS projects
- `Retainer_Monthly_Hours__c` — monthly hours for Retainer projects
- `Name` — project name; checked for `[DC to DC]` and `[Retainer]` prefixes

**MPM4_BASE__Milestone1_Milestone__c** (milestones)
- `Name` — milestone name; "Service Sales and Planning" is the key one for acquisition
- `MPM4_BASE__Project__c` — linked project

**MPM4_BASE__Milestone1_Task__c** (tasks)
- `MPM4_BASE__Project_Lookup__c` — linked project
- `MPM4_BASE__Project_Milestone__c` — linked milestone

**MPM4_BASE__Milestone1_Time__c** (time entries)
- `MPM4_BASE__Project_Task__c` — linked task
- `MPM4_BASE__Project__c` — linked project
- `MPM4_BASE__Hours__c` — hours logged
- `MPM4_BASE__Date__c` — date of the entry (used for retainer quarter filtering)
- `MPM4_BASE__Start__c` / `MPM4_BASE__Stop__c` — datetime range
- `CreatedById` — the SF user who created this entry (identifies who logged the time)
- `CreatedBy.Name` — display name of the logger

---

## Key Functions (kpi_server.py)

### Auth
- `get_cli_token()` — runs `sf org display` and returns `(access_token, instance_url)`
- `resolve_identity()` — calls userinfo endpoint, stores into Flask session
- `get_sf()` — builds `Salesforce()` connection from session

### KPI Helpers
- `get_quarter_bounds(year, quarter)` → `(datetime_start, datetime_end)`
- `delivery_pts_by_hours(hrs)` → bracket points from `PROJECT_SIZE_POINTS`
- `dc_to_dc_pts(proj_name)` → 7, 4, or None
- `is_retainer(proj_name)` → bool; checks for `[retainer]` in name (case-insensitive)
- `deal_type_for(opp_name, deal_type_raw)` → `"SaaS"` or `"CPaaS"`
- `acq_pts(segment, opp_type, board, deal_type)` → float base acquisition pts

### Data Fetchers
- `get_logged_hours(sf, project_id, user_id)` → `(my_hrs, other_hrs, other_names)` — all-time hours on a project
- `get_logged_hours_in_quarter(sf, project_id, user_id, qs, qe)` → same but date-filtered (used for retainers)
- `get_projects_by_time_entries(sf, user_id, qs, qe)` → completed projects where user logged time but is NOT the named SE (assisted delivery)
- `get_retainer_projects(sf, user_id, qs, qe)` → active retainer projects where user logged time in quarter
- `get_services_sales_hours_bulk(sf, opp_ids, user_id)` → `{opp_id: (my_hrs, other_hrs, other_names)}` for SS&P milestone hours
- `get_additional_acq_opps(sf, user_id, qs, qe)` → Closed Won opps in quarter where user has SS&P hours but is not named SE

### Main Entry
- `calculate_kpi(sf, user_id, year, quarter)` → dict with `acquisition`, `delivery`, `pipeline`, `summary`

---

## SOQL Constraints to Know

**No nested subqueries in OR clauses.** This is a Salesforce limitation.

WRONG — will throw `MALFORMED_QUERY`:
```sql
WHERE Sales_Engineer__c = 'X' OR MPM4_BASE__Opportunity__c NOT IN (SELECT Id FROM Opportunity WHERE ...)
```

CORRECT — fetch the IDs separately and filter in Python:
```python
my_opp_ids = {o["Id"] for o in sf.query_all("SELECT Id FROM Opportunity WHERE Sales_Engineer__c = ...")["records"]}
return [p for p in results if p.get("MPM4_BASE__Opportunity__c") not in my_opp_ids]
```

**Batch IN clauses in 200s.** SOQL has limits on IN clause length. All dynamic ID lists are chunked:
```python
for i in range(0, len(ids), 200):
    chunk = ",".join(f"'{x}'" for x in ids[i:i+200])
    sf.query_all(f"... WHERE Id IN ({chunk})")
```

**Use `Date_Completed__c` for project quarter filtering**, not `LastModifiedDate` or `CloseDate`.

---

## Flask Routes

| Route    | Method | Auth required | Description |
|----------|--------|---------------|-------------|
| `/`      | GET    | Yes           | Dashboard HTML (renders template) |
| `/login` | GET    | No            | Login page — renders HTML only, does NOT authenticate |
| `/auth`  | GET    | No            | Runs SF CLI auth, sets session, redirects to `/` |
| `/logout`| GET    | No            | Clears session keys, redirects to `/login` |
| `/api/kpi` | GET  | Yes           | JSON — accepts `?year=&quarter=`, returns full KPI data |

---

## Data Flow (API request)

```
GET /api/kpi?year=2026&quarter=1
  → calculate_kpi(sf, user_id, 2026, 1)
    → Acquisition:
        my_opps (named SE, Closed Won Q1)
        + additional_acq_opps (SS&P time logged, not named SE)
        → get_services_sales_hours_bulk() for all opps
        → proportional split
    → Delivery:
        my_projects (Completed, SE on opp)
        + get_projects_by_time_entries() (Completed, assisted)
        → filter out [Retainer] projects
        → get_logged_hours() per project
        → dc_to_dc_pts() or bracket scoring
    → Retainer:
        get_retainer_projects() (Active, [Retainer] in name)
        → get_logged_hours_in_quarter() per project
        → 75% rule + bracket
    → Pipeline:
        active projects (not Completed/Terminated, SE on opp)
        → estimated pts if completed now
    → summary: acq_total, del_total, confirmed, pipeline_pts, projected
```

---

## Common Issues

**"Session expired" error on dashboard**: Re-run `sf login -o infobip` in a terminal, then refresh and click "Login with Salesforce".

**500 error on Calculate**: The JSON response includes a `traceback` field — open DevTools → Network → click the failed `/api/kpi` request → read the Response body to see the full Python traceback.

**Retainer projects not showing**: Check that the SF project name literally contains `[Retainer]` (with square brackets). The detection is `"[retainer]" in proj_name.lower()`.

**DC to DC points not showing**: Project name must contain `[DC to DC SaaS]` or `[DC to DC CPaaS]` (with square brackets, case-insensitive).

**Service Sales & Planning hours not found**: The milestone name in SF is "Services Sales & Planning". The SOQL filter is `MPM4_BASE__Project_Milestone__r.Name LIKE '%Service%Sales%'` (using `%Service%Sales%` to match both "Service Sales" and "Services Sales" variants).
