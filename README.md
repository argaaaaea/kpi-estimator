# SE KPI Dashboard

A tool for Infobip Solution Engineers to estimate KPI incentive points from Salesforce data. Includes a web dashboard and a CLI estimator.

---

## Prerequisites

Before you begin, make sure you have the following installed:

- **Python 3.9+** — [python.org](https://www.python.org/downloads/)
- **Salesforce CLI** — [developer.salesforce.com/tools/salesforcecli](https://developer.salesforce.com/tools/salesforcecli)
- **Git** — [git-scm.com](https://git-scm.com/)

---

## Step 1 — Fork and clone the repository

1. Go to [https://github.com/argaaaaea/kpi-estimator](https://github.com/argaaaaea/kpi-estimator)
2. Click **Fork** (top-right) to create your own copy
3. Clone your fork:
   ```bash
   git clone https://github.com/<your-username>/kpi-estimator.git
   cd kpi-estimator
   ```

---

## Step 2 — Install Python dependencies

```bash
pip install flask simple_salesforce requests rich
```

---

## Step 3 — Log in to Salesforce CLI

Authenticate with your Salesforce org using the CLI. Replace `<alias>` with a short name you want to use (e.g. `infobip`):

```bash
sf login -o <alias>
```

A browser window will open — complete the Salesforce login there. Once done, return to the terminal.

To verify it worked:
```bash
sf org display -o <alias>
```

You should see your org details including an access token.

---

## Step 4 — Create your config file

Copy the example config:

```bash
cp config.example.py config.py
```

Then open `config.py` and set your alias to match what you used in Step 3:

```python
SF_ALIAS = "<alias>"   # e.g. "infobip"
```

> `config.py` is gitignored — it will never be committed.

---

## Step 5 — Run the tool

### Web Dashboard

```bash
python kpi_server.py
```

Open your browser at [http://localhost:5000](http://localhost:5000).

Click **Login with Salesforce**, select a quarter, then click **Calculate KPI**.

### CLI Estimator

```bash
python kpi_estimate.py
```

Prints your KPI estimate for the current quarter directly in the terminal.

---

## Troubleshooting

**"Session expired" on the dashboard**
Re-run `sf login -o <alias>` in a terminal, then refresh the page and log in again.

**500 error on Calculate**
Open browser DevTools → Network tab → click the failed `/api/kpi` request → read the Response body for the full error traceback.

**Retainer or DC to DC projects not showing**
Check that the Salesforce project name contains the exact prefix (with square brackets):
- Retainer: `[Retainer]`
- DC to DC: `[DC to DC SaaS]` or `[DC to DC CPaaS]`
