app_name = "vyogo_probe"
app_title = "Vyogo Probe"
app_publisher = "Vyogo Technologies"
app_description = "Observable targets for every frappe-operator custom resource"
app_email = "hello@vyogo.tech"
app_license = "MIT"

# SiteMigration: after_migrate runs on every `bench migrate`; the patch in
# patches.txt runs once. Both leave a Probe Record behind.
after_migrate = ["vyogo_probe.tasks.record_migrate"]

# The site scheduler (a bench pod) writes a heartbeat record every hour.
scheduler_events = {"hourly": ["vyogo_probe.tasks.heartbeat"]}

# SiteServerScript / SiteWebhook target Probe Record events.
doc_events = {"Probe Record": {"after_insert": "vyogo_probe.tasks.on_probe_insert"}}
