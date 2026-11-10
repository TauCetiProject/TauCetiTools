#!/usr/bin/env python3
"""Dedicated placement and same-run fallback are bounded and fail closed."""

import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import fleet_runner
from fleet_recovery import recover
from fleet_runner import choose, failed_placements, ranked_runners, resolve_machines, route, scan_capacity


def runner(*, busy=False, status="online", labels=None):
    return dict(
        busy=busy,
        status=status,
        labels=[
            dict(name=n)
            for n in (labels or ["self-hosted", "linux", "x64", "tauceti-fleet"])
        ],
    )


class Selection(unittest.TestCase):
    def ranked_fixture(self):
        machines = [dict(name="tauchonki", machine_id="a" * 32, max_jobs=1, kinds=["staging"]),
                    dict(name="chris", machine_id="b" * 32, max_jobs=2, kinds=["pr", "staging"])]
        def guest(machine, identity):
            labels = ["self-hosted", "linux", "x64", "fleet-" + machine["machine_id"],
                      "fleet-instance-" + str(identity) * 16]
            labels += ["tauceti-fleet-" + k for k in machine["kinds"]]
            return dict(runner(labels=labels), id=identity)
        runners = [guest(machines[1], 3), guest(machines[0], 1), guest(machines[1], 2)]
        return machines, [dict(total_count=3, runners=runners)]

    def test_ordered_machine_selection_enforces_class_and_exact_instance(self):
        machines, pages = self.ranked_fixture()
        staging = ranked_runners(pages, kind="staging", machines=machines)
        self.assertEqual([r["machine"] for r in staging], ["tauchonki", "chris", "chris"])
        pr = ranked_runners(pages, kind="pr", machines=list(reversed(machines)))
        self.assertEqual([r["machine"] for r in pr], ["chris", "chris"])
        self.assertTrue(all("tauceti-fleet-pr" in r["labels"] and "fleet-" + "b" * 32 in r["labels"] for r in pr))
        self.assertEqual(pr[0]["runner_id"], 2)

    def test_busy_and_reserved_instances_are_unavailable(self):
        machines, pages = self.ranked_fixture()
        pages[0]["runners"][0]["busy"] = True
        result = ranked_runners(pages, kind="staging", machines=machines, reserved=[3])
        self.assertEqual([r["runner_id"] for r in result], [1, 2])
        result = ranked_runners(pages, kind="staging", machines=machines, reserved=[2, 3])
        self.assertEqual([r["runner_id"] for r in result], [1])

    def test_different_runs_spread_across_idle_guests_despite_unknown_optional_queue_inventory(self):
        machines, pages = self.ranked_fixture()
        def select(key, queued):
            return route(kind="pr", order=["chris", "namespace", "github"], machines={m["name"]: m for m in machines},
                         runner_pages=pages, observed=dict(github_busy=0, namespace_busy=0, queued_fleet=queued),
                         github_budget=18, namespace_cap=20, selection_key=key)
        self.assertEqual({select(str(n), [])["runner_id"] for n in range(8)}, {2, 3})
        self.assertEqual(select("1", None)["pool"], "chris")

    def test_staging_uses_reserved_guest_before_shared_guest_for_every_selection_key(self):
        machines, pages = self.ranked_fixture()
        machine = dict(machines[1], max_jobs=2)
        shared, dedicated = pages[0]["runners"][0], pages[0]["runners"][2]
        # The shared guest has the older/lower ID, matching the production case.
        shared["id"], dedicated["id"] = 2, 3
        dedicated["labels"] = [label for label in dedicated["labels"] if label["name"] != "tauceti-fleet-pr"]
        pages = [dict(total_count=2, runners=[shared, dedicated])]
        def select(kind="staging", key="", queued=()):
            return route(kind=kind, order=["chris", "namespace"], machines={"chris": machine},
                         runner_pages=pages, observed=dict(github_busy=0, namespace_busy=0, queued_fleet=list(queued)),
                         github_budget=18, namespace_cap=20, selection_key=key)
        for key in map(str, range(32)):
            self.assertEqual(select(key=key)["runner_id"], 3)
        self.assertEqual(select("pr")["runner_id"], 2)
        # Old dispatcher settings cannot hide a registered staging reservation.
        machine.update(max_jobs=1, kinds=["pr"])
        self.assertEqual(select()["runner_id"], 3)
        dedicated["busy"] = True
        self.assertEqual(select()["runner_id"], 2)
        dedicated["busy"] = False
        dedicated["status"] = "offline"
        self.assertEqual(select()["runner_id"], 2)
        dedicated["status"] = "online"
        target = {label["name"] for label in dedicated["labels"]}
        self.assertEqual(select(queued=[target])["runner_id"], 2)

    def test_legacy_labels_partial_inventories_and_ambiguous_instances_cannot_authorize_a_slot(self):
        machines, pages = self.ranked_fixture()
        pages[0]["runners"][1]["labels"] = [dict(name=n) for n in ["self-hosted", "linux", "x64", "tauceti-fleet", "fleet-" + "a" * 32]]
        self.assertEqual([r["machine"] for r in ranked_runners(pages, kind="staging", machines=machines)], ["chris", "chris"])
        pages[0]["total_count"] = 4
        with self.assertRaises(ValueError):
            ranked_runners(pages, kind="staging", machines=machines)

    def test_existing_dispatcher_orders_cloud_and_machine_choices_by_job_class(self):
        machines, pages = self.ranked_fixture()
        configured = {m["name"]: m for m in machines}
        def select(kind, gh, ns, *, reserved=()):
            order = ["github", "chris", "tauchonki", "namespace"] if kind == "pr" else ["tauchonki", "chris", "namespace", "github"]
            return route(kind=kind, order=order, machines=configured, runner_pages=pages,
                         observed=dict(github_busy=gh, namespace_busy=ns, queued_fleet=list(reserved)),
                         github_budget=18, namespace_cap=20)
        self.assertEqual(select("pr", 0, 0)["pool"], "github")
        self.assertEqual(select("pr", 19, 0)["pool"], "chris")
        self.assertEqual(select("staging", 0, 0)["pool"], "tauchonki")
        pages[0]["runners"][1]["busy"] = True
        self.assertEqual(select("staging", 0, 0)["pool"], "chris")
        for r in pages[0]["runners"]:
            r["busy"] = True
        self.assertEqual(select("staging", 0, 19)["pool"], "namespace")
        self.assertEqual(select("staging", 0, 20)["pool"], "github")
        self.assertEqual(select("pr", 19, 19)["pool"], "namespace")
        self.assertEqual(select("pr", 19, 20), dict(pool="github", waiting=True))

    def test_expanded_brett_capacity_keeps_other_ready_machines_available(self):
        machines, pages = self.ranked_fixture()
        machines[1].update(name="brett", max_jobs=24)
        for guest in pages[0]["runners"]:
            if guest["id"] in (2, 3):
                guest["labels"].append(dict(name="fleet-host-brett-server"))
        config = {"tauchonki": machines[0], "brett": dict(enrollment_name="brett-server", max_jobs=24, kinds=["pr", "staging"])}
        def select():
            return route(kind="staging", order=["tauchonki", "brett", "namespace", "github"], machines=config,
                         runner_pages=pages, observed=dict(github_busy=20, namespace_busy=20, queued_fleet=[]),
                         github_budget=12, namespace_cap=20)
        self.assertEqual(select()["pool"], "tauchonki")
        pages[0]["runners"][1]["busy"] = True
        self.assertEqual(select()["pool"], "brett")

    def test_registered_capacity_and_classes_override_stale_or_missing_dispatcher_limits(self):
        machines, _ = self.ranked_fixture()
        machine = dict(machines[1], name="brett", max_jobs=2, kinds=["staging"])
        def guest(identity, busy=False):
            return dict(runner(busy=busy, labels=["self-hosted", "linux", "x64", "fleet-" + machine["machine_id"],
                "fleet-instance-" + f"{identity:016x}", "tauceti-fleet-pr", "tauceti-fleet-staging"]), id=identity)
        rows = [guest(n, n <= 2) for n in range(1, 25)]
        pages = [dict(total_count=len(rows), runners=rows)]
        # Both classes use all 21 idle, unreserved runners though the old cap is full
        # and the old class list says staging only.
        for config in [machine, {k: machine[k] for k in ("name", "machine_id")}]:
            for kind in ("pr", "staging"):
                ready = ranked_runners(pages, kind=kind, machines=[config], reserved=[3])
                self.assertEqual([r["runner_id"] for r in ready], list(range(4, 25)))

    def test_queued_exact_targets_exclude_their_registered_instances(self):
        machines, pages = self.ranked_fixture()
        queued = [{label["name"] for label in r["labels"]} for r in pages[0]["runners"] if r["id"] in (2, 3)]
        selected = route(kind="pr", order=["github", "chris", "namespace"], machines={m["name"]: m for m in machines},
                         runner_pages=pages, observed=dict(github_busy=19, namespace_busy=0, queued_fleet=queued),
                         github_budget=18, namespace_cap=20)
        self.assertEqual(selected["pool"], "namespace")

    def test_enrollment_label_resolves_one_permanent_machine_and_ambiguity_disables_it(self):
        _, pages = self.ranked_fixture()
        for r in pages[0]["runners"]:
            if r["id"] in (2, 3):
                r["labels"].append(dict(name="fleet-host-chris-server"))
        config = dict(chris=dict(enrollment_name="chris-server", max_jobs=2, kinds=["pr", "staging"]))
        resolved = resolve_machines(config, pages[0]["runners"])
        self.assertEqual(resolved["chris"]["machine_id"], "b" * 32)
        pages[0]["runners"][1]["labels"].append(dict(name="fleet-host-chris-server"))
        self.assertFalse(resolve_machines(config, pages[0]["runners"]))


    def test_idle_host_preferred_and_busy_offline_hosts_excluded(self):
        for runners, expected in [
            ([runner()], True),
            ([runner(busy=True)], False),
            ([runner(status="offline")], False),
            ([runner(busy=True), runner()], True),
            ([runner(labels=["ubuntu-latest"])], False),
        ]:
            self.assertEqual(
                choose(
                    [dict(total_count=len(runners), runners=runners)],
                    enabled=True,
                    attempt=1,
                ),
                expected,
            )

    def test_partial_inventory_disabled_feature_and_retries_use_namespace(self):
        page = dict(total_count=1, runners=[runner()])
        self.assertFalse(choose([page], enabled=False, attempt=1))
        self.assertFalse(choose([page], enabled=True, attempt=2))
        self.assertFalse(
            choose([dict(total_count=2, runners=[runner()])], enabled=True, attempt=1)
        )

    def test_retries_skip_never_acquired_machines_but_keep_hosts_that_finished_real_work(self):
        failed = dict(name="sandboxed-build", status="completed", conclusion="cancelled", runner_id=0,
                      runner_name="", labels=["self-hosted", "fleet-" + "a" * 32])
        def read(path):
            self.assertIn("/attempts/1/jobs?", path)
            return dict(total_count=1, jobs=[failed])
        excluded = failed_placements(read, "Test/code", 42, 2)
        self.assertEqual(excluded, {"a" * 32})
        machines, pages = self.ranked_fixture()
        selected = route(kind="staging", order=["tauchonki", "chris", "namespace", "github"],
                         machines={m["name"]: m for m in machines}, runner_pages=pages,
                         observed=dict(github_busy=0, namespace_busy=0, queued_fleet=[]),
                         github_budget=18, namespace_cap=20, excluded=excluded)
        self.assertEqual(selected["pool"], "chris")
        failed["runner_id"] = 1
        self.assertFalse(failed_placements(read, "Test/code", 42, 2))


class LiveMembership(unittest.TestCase):
    def guest(self, identity="a"*32, number=1, kinds=("pr",)):
        return dict(id=number,status="online",busy=False,labels=[dict(name=n) for n in
            ["self-hosted","linux","x64","fleet-"+identity,"fleet-instance-"+f'{number:016x}',
             *["tauceti-fleet-"+kind for kind in kinds]]])

    def select(self, guests, *, kind="pr", excluded=(), queued=(), total=None, order=None):
        return route(kind=kind,order=order or ["github","fleet","namespace"],machines={},
            runner_pages=[dict(total_count=len(guests) if total is None else total,runners=guests)],
            observed=dict(github_busy=19,namespace_busy=0,queued_fleet=list(queued)),
            github_budget=18,namespace_cap=20,excluded=excluded)

    def test_new_host_and_replacement_join_without_configuration_edits(self):
        first=self.select([self.guest()])
        self.assertEqual(first['pool'],'fleet')
        self.assertEqual(first['machine_id'],'a'*32)
        replacement=self.select([self.guest('b'*32,2)])
        self.assertEqual(replacement['machine_id'],'b'*32)
        self.assertIn('fleet-instance-0000000000000002',replacement['labels'])
        self.assertEqual(self.select([])['pool'],'namespace')

    def test_busy_offline_and_queued_slots_do_not_advertise_capacity(self):
        for field,value in [('busy',True),('status','offline')]:
            guest=self.guest();guest[field]=value
            self.assertEqual(self.select([guest])['pool'],'namespace')
        guest=self.guest()
        labels={v['name'] for v in guest['labels']}
        self.assertEqual(self.select([guest],queued=[labels])['pool'],'namespace')

    def test_class_labels_and_staging_reservation_priority_are_preserved(self):
        pr=self.guest();mixed=self.guest('b'*32,2,('pr','staging'));staging=self.guest('c'*32,3,('staging',))
        self.assertEqual(self.select([pr],kind='staging')['pool'],'namespace')
        self.assertEqual(self.select([staging],kind='pr')['pool'],'namespace')
        self.assertEqual(self.select([mixed,staging],kind='staging')['runner_id'],3)
        self.assertEqual(self.select([pr],kind='other')['pool'],'namespace')

    def test_retry_exclusions_apply_to_individual_discovered_hosts(self):
        guests=[self.guest(),self.guest('b'*32,2)]
        self.assertEqual(self.select(guests,excluded={'a'*32})['machine_id'],'b'*32)
        self.assertEqual(self.select(guests,excluded={'a'*32,'b'*32})['pool'],'namespace')
        self.assertEqual(self.select(guests,excluded={'fleet'})['pool'],'namespace')

    def test_incomplete_or_ambiguous_inventory_cannot_select_a_runner(self):
        self.assertEqual(self.select([self.guest()],total=2)['pool'],'namespace')
        self.assertEqual(self.select([self.guest(),self.guest()])['pool'],'namespace')
        for label in ['fleet-'+'b'*32,'fleet-instance-0000000000000002']:
            ambiguous=self.guest();ambiguous['labels'].append(dict(name=label))
            # Include a valid peer on the same machine so discovery alone cannot mask ambiguity.
            selected=self.select([ambiguous,self.guest(number=3)])
            self.assertEqual(selected['runner_id'],3)

    def test_generic_membership_is_opt_in(self):
        self.assertEqual(self.select([self.guest()],order=['github','namespace'])['pool'],'namespace')


class CapacityScan(unittest.TestCase):
    def test_namespace_builds_include_queued_jobs_and_exclude_picker_and_control_jobs(self):
        ns = ["nscloud-ubuntu-24.04-amd64-8x16-with-features"]
        fixtures = {
            "orgs/Test/repos?per_page=100&page=1": [dict(name="code", archived=False)],
            "repos/Test/code/actions/runs?status=in_progress&per_page=100&page=1": dict(total_count=1, workflow_runs=[dict(id=1)]),
            "repos/Test/code/actions/runs?status=queued&per_page=100&page=1": dict(total_count=1, workflow_runs=[dict(id=2)]),
            "repos/Test/code/actions/runs/1/jobs?per_page=100&page=1": dict(total_count=5, jobs=[
                dict(name="sandboxed-build", status="in_progress", labels=ns),
                dict(name="pick-runner / pick", status="in_progress", labels=ns),
                dict(name="finalize-merge-group-build", status="in_progress", labels=ns),
                dict(name="short-control", status="in_progress", labels=["ubuntu-latest"]),
                dict(name="completed-build", status="completed", labels=["ubuntu-latest"])]),
            "repos/Test/code/actions/runs/2/jobs?per_page=100&page=1": dict(total_count=1, jobs=[
                dict(name="sandboxed-build", status="queued", labels=ns)]),
        }
        result = scan_capacity(fixtures.__getitem__, repository="Test/code")
        self.assertEqual(result, dict(github_busy=1, namespace_busy=2, queued_fleet=[]))
        fixtures["repos/Test/code/actions/runs/2/jobs?per_page=100&page=1"]["total_count"] = 2
        fixtures["repos/Test/code/actions/runs/2/jobs?per_page=100&page=2"] = dict(total_count=2, jobs=[])
        with self.assertRaises(ValueError):
            scan_capacity(fixtures.__getitem__, repository="Test/code")


class RoutingEntrypoint(unittest.TestCase):
    """Exercise actual quota guards, fallback scans and workflow outputs offline."""
    def fixture(self, *, remaining=5000, busy=0):
        return {
            "rate_limit": dict(resources=dict(core=dict(remaining=remaining))),
            "orgs/Test/repos?per_page=100&page=1": [dict(name="code", archived=False)],
            "repos/Test/code/actions/runs?status=in_progress&per_page=100&page=1": dict(total_count=1, workflow_runs=[dict(id=3)]),
            "repos/Test/code/actions/runs?status=queued&per_page=100&page=1": dict(total_count=0, workflow_runs=[]),
            "repos/Test/code/actions/runs/3/jobs?per_page=100&page=1": dict(total_count=busy, jobs=[dict(name="sandboxed-build", status="in_progress", labels=["ubuntu-24.04"]) for _ in range(busy)]),
        }

    def pick(self, fixtures, *, config=None, **overrides):
        calls = []
        shared_urls = []
        class Response(io.BytesIO):
            headers = {}
        class Opener:
            def open(self, request, timeout):
                token = request.get_header("Authorization").removeprefix("Bearer ")
                path = "shared" if request.full_url.startswith("https://github-state.taucetiproject.org/") else request.full_url.removeprefix("https://api.github.com/")
                calls.append((token, path))
                if path == "shared":
                    shared_urls.append(request.full_url)
                value = fixtures[token][path]
                if callable(value):
                    value = value()
                if isinstance(value, Exception):
                    raise value
                headers = {}
                if isinstance(value, tuple):
                    value, headers = value
                response = Response(json.dumps(value).encode())
                response.headers = headers
                return response
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "tauceti.toml").write_text(config or '[services.fleet]\nnamespace_build_cap=20\npr_order=["github","namespace"]\nstaging_order=["namespace","github"]\n')
            output = root / "output"
            environment = dict(GITHUB_REPOSITORY="Test/code", GITHUB_RUN_ID="5", GITHUB_RUN_ATTEMPT="1",
                               GITHUB_OUTPUT=str(output), FLEET_JOB_KIND="pr", GH_TOKEN="app", FALLBACK_TOKEN="repository")
            environment.update(overrides)
            before = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, environment, clear=True), patch.object(fleet_runner.urllib.request, "build_opener", return_value=Opener()), redirect_stdout(io.StringIO()) as captured:
                    fleet_runner.route_main()
                    self.last_log = captured.getvalue()
                    self.last_shared_urls = shared_urls
            finally:
                os.chdir(before)
            return dict(line.split("=", 1) for line in output.read_text().splitlines()), calls

    def staging_fixture(self):
        machines, inventory = Selection().ranked_fixture()
        config = '[services.fleet]\nstaging_order=["tauchonki","chris","namespace","github"]\n'
        for machine in machines:
            config += f'[services.fleet.machines.{machine["name"]}]\nmachine_id="{machine["machine_id"]}"\nmax_jobs={machine["max_jobs"]}\nkinds={json.dumps(machine["kinds"])}\n'
        return config, inventory[0]

    def test_generic_pr_pool_targets_the_group_and_class_across_runner_replacement(self):
        guest=LiveMembership().guest()
        endpoint='orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1'
        config='[services.fleet]\npr_order=["fleet","namespace","github"]\n'
        output,calls=self.pick(dict(fleet={endpoint:dict(total_count=1,runners=[guest])},
            state=dict(shared=self.shared())),config=config,FLEET_CI_ENABLED='true',
            FLEET_READ_TOKEN='fleet',FLEET_RUNNER_GROUP_ID='3',GITHUB_STATE_TOKEN='state')
        self.assertEqual(output['pool'],'fleet')
        target=json.loads(output['runner'])
        self.assertEqual(target['group'],'TauCetiFleet')
        self.assertEqual(set(target['labels']), {'self-hosted', 'linux', 'x64', 'tauceti-fleet-pr'})
        # The original guest can disappear: another eligible host still matches.
        replacement = LiveMembership().guest('b'*32, 2)
        self.assertLessEqual(set(target['labels']), {label['name'] for label in replacement['labels']})
        staging = LiveMembership().guest('c'*32, 3, ('staging',))
        self.assertFalse(set(target['labels']) <= {label['name'] for label in staging['labels']})
        self.assertEqual([p for token,p in calls if token=='fleet'],[endpoint])
        self.assertFalse(any(token in ('app','repository') for token,_ in calls))

    def test_pr_pool_retry_needs_no_cancelled_placement_history(self):
        guest = LiveMembership().guest()
        endpoint = 'orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1'
        output, calls = self.pick(dict(fleet={endpoint: dict(total_count=1, runners=[guest])},
            state=dict(shared=self.shared())),
            config='[services.fleet]\npr_order=["fleet","namespace","github"]\n',
            FLEET_CI_ENABLED='true', FLEET_READ_TOKEN='fleet', FLEET_RUNNER_GROUP_ID='3',
            GITHUB_STATE_TOKEN='state', GITHUB_RUN_ATTEMPT='2')
        self.assertEqual(output['pool'], 'fleet')
        self.assertEqual(set(json.loads(output['runner'])['labels']),
                         {'self-hosted', 'linux', 'x64', 'tauceti-fleet-pr'})
        self.assertFalse(any('attempts/' in path or path == 'rate_limit' for _, path in calls))

    def test_generic_pool_remains_disabled_without_explicit_fleet_routing(self):
        config='[services.fleet]\npr_order=["fleet","namespace","github"]\n'
        output,calls=self.pick(dict(state=dict(shared=self.shared(routing_version=2,namespace_busy=0,queued_fleet=[]))),
            config=config,GITHUB_STATE_TOKEN='state')
        self.assertEqual(output['pool'],'namespace')
        self.assertFalse(any('runner-groups' in path for _,path in calls))

    def test_staging_v1_and_missing_shared_state_choose_fleet_without_cloud_scans(self):
        config, inventory = self.staging_fixture()
        for state in (self.shared(), OSError("shared server offline")):
            fixtures = dict(fleet={"orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1": inventory}, state=dict(shared=state))
            output, calls = self.pick(fixtures, config=config, FLEET_JOB_KIND="staging", FLEET_CI_ENABLED="true", FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3", GITHUB_STATE_TOKEN="state")
            self.assertEqual(output["pool"], "tauchonki")
            self.assertEqual(output["namespace_busy"], "")
            self.assertFalse(any(token in ("app", "repository") for token, _ in calls), calls)
            self.assertTrue(any(label.startswith("fleet-instance-") for label in json.loads(output["runner"])["labels"]))

    def test_staging_known_reservations_still_select_next_eligible_machine(self):
        config, inventory = self.staging_fixture()
        queued = [[v["name"] for v in inventory["runners"][1]["labels"]]]
        output, calls = self.pick(dict(fleet={"orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1": inventory}, state=dict(shared=self.shared(routing_version=2, namespace_busy=0, queued_fleet=queued))), config=config, FLEET_JOB_KIND="staging", FLEET_CI_ENABLED="true", FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3", GITHUB_STATE_TOKEN="state")
        self.assertEqual(output["pool"], "chris")
        self.assertEqual(len(calls), 2)

    def test_direct_scan_finishes_before_pr_runner_availability_is_read(self):
        config, inventory = self.staging_fixture()
        config = config.replace('[services.fleet]\n', '[services.fleet]\npr_order=[\"github\",\"chris\",\"tauchonki\",\"namespace\"]\n')
        fixtures = self.fixture(busy=19)
        output, calls = self.pick(dict(app=fixtures, fleet={"orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1": inventory}), config=config, FLEET_CI_ENABLED="true", FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3", PR_HEAD="a"*40)
        self.assertEqual(output["pool"], "chris")
        self.assertGreater(calls.index(("fleet", "orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1")), calls.index(("app", "repos/Test/code/actions/runs/3/jobs?per_page=100&page=1")))

    def test_pr_uses_available_machine_before_unneeded_cloud_scan_without_dispatch_head(self):
        config, inventory = self.staging_fixture()
        config = config.replace('[services.fleet]\n', '[services.fleet]\npr_order=["github","chris","tauchonki","namespace"]\n')
        output, calls = self.pick(dict(
            state=dict(shared=self.shared(busy=19)),
            fleet={"orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1": inventory},
        ), config=config, FLEET_CI_ENABLED="true", FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3",
           GITHUB_STATE_TOKEN="state", PR_HEAD="")
        self.assertEqual(output["pool"], "chris")
        labels = json.loads(output["runner"])["labels"]
        self.assertIn("fleet-" + "b" * 32, labels)
        self.assertFalse(any(label.startswith("fleet-instance-") for label in labels))
        self.assertEqual([token for token, _ in calls], ["state", "fleet"])

    def test_unknown_cloud_does_not_delay_an_available_machine_with_another_scan(self):
        config, inventory = self.staging_fixture()
        config = config.replace('[services.fleet]\n', '[services.fleet]\npr_order=["namespace","chris","github"]\n')
        output, calls = self.pick(dict(
            state=dict(shared=self.shared(busy=19)), app=self.fixture(busy=19),
            fleet={"orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1": inventory},
        ), config=config, FLEET_CI_ENABLED="true", FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3",
           GITHUB_STATE_TOKEN="state")
        self.assertEqual(output["pool"], "chris")
        self.assertFalse(any(token == "app" for token, _ in calls))

    def test_available_first_cloud_does_not_fetch_unneeded_fleet_inventory(self):
        config, inventory = self.staging_fixture()
        config = config.replace('[services.fleet]\n', '[services.fleet]\npr_order=["github","chris","namespace"]\n')
        output, calls = self.pick(dict(state=dict(shared=self.shared())), config=config,
            FLEET_CI_ENABLED="true", FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3", GITHUB_STATE_TOKEN="state")
        self.assertEqual(output["pool"], "github")
        self.assertEqual(calls, [("state", "shared")])

    def test_staging_excluded_unknown_cloud_does_not_delay_next_machine(self):
        config, inventory = self.staging_fixture()
        config = config.replace('staging_order=["tauchonki","chris","namespace","github"]', 'staging_order=["namespace","chris","github"]')
        app = self.fixture(busy=19)
        app["repos/Test/code/actions/runs/5/attempts/1/jobs?per_page=100&page=1"] = dict(total_count=1, jobs=[
            dict(name="sandboxed-build", status="completed", conclusion="cancelled", runner_id=0,
                 runner_name="", labels=["nscloud-ubuntu-24.04-amd64-8x16-with-features"])])
        output, calls = self.pick(dict(app=app, state=dict(shared=self.shared(busy=19)),
            fleet={"orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1": inventory}),
            config=config, FLEET_CI_ENABLED="true", FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3",
            GITHUB_STATE_TOKEN="state", GITHUB_RUN_ATTEMPT="2", FLEET_JOB_KIND="staging")
        self.assertEqual(output["pool"], "chris")
        self.assertFalse(any("orgs/Test/repos" in path for _, path in calls))

    def test_cloud_absent_from_order_does_not_trigger_discovery_when_everything_is_full(self):
        config, inventory = self.staging_fixture()
        config = config.replace('[services.fleet]\n', '[services.fleet]\npr_order=["github","chris"]\n')
        for guest in inventory["runners"]:
            guest["busy"] = True
        output, calls = self.pick(dict(state=dict(shared=self.shared(busy=19)),
            fleet={"orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1": inventory}),
            config=config, FLEET_CI_ENABLED="true", FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3",
            GITHUB_STATE_TOKEN="state")
        self.assertEqual(output["pool"], "github")
        self.assertEqual([token for token, _ in calls], ["state", "fleet"])

    def test_staging_fallthrough_refreshes_runner_availability_after_cloud_scan(self):
        config, inventory = self.staging_fixture()
        busy = json.loads(json.dumps(inventory))
        for guest in busy["runners"]: guest["busy"] = True
        snapshots = [busy, inventory]
        output, calls = self.pick(dict(app=self.fixture(), fleet={"orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1": lambda: snapshots.pop(0)}), config=config, FLEET_JOB_KIND="staging", FLEET_CI_ENABLED="true", FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3")
        self.assertEqual(output["pool"], "tauchonki")
        self.assertEqual([token for token, _ in calls if token=="fleet"], ["fleet", "fleet"])

    def test_staging_retry_checks_prior_unassigned_placement_before_reusing_fleet(self):
        config, inventory = self.staging_fixture()
        fixtures = self.fixture()
        fixtures["repos/Test/code/actions/runs/5/attempts/1/jobs?per_page=100&page=1"] = dict(total_count=1, jobs=[dict(name="sandboxed-build", status="completed", conclusion="cancelled", runner_id=0, runner_name="", labels=["self-hosted", "fleet-" + "a" * 32])])
        output, calls = self.pick(dict(app=fixtures, fleet={"orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1": inventory}, state=dict(shared=self.shared(routing_version=2, namespace_busy=0, queued_fleet=[]))), config=config, FLEET_JOB_KIND="staging", FLEET_CI_ENABLED="true", FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3", GITHUB_STATE_TOKEN="state", GITHUB_RUN_ATTEMPT="2")
        self.assertEqual(output["pool"], "chris")
        self.assertIn(("app", "repos/Test/code/actions/runs/5/attempts/1/jobs?per_page=100&page=1"), calls)

    def test_missing_retry_history_does_not_veto_available_machines_or_change_order(self):
        config, inventory = self.staging_fixture()
        for first_busy, expected in ((False, "tauchonki"), (True, "chris")):
            with self.subTest(first_busy=first_busy):
                current = json.loads(json.dumps(inventory))
                current["runners"][1]["busy"] = first_busy
                fixtures = dict(
                    app=self.fixture(remaining=1500),
                    repository=self.fixture(remaining=250),
                    fleet={"orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1": current},
                    state=dict(shared=self.shared(routing_version=2, namespace_busy=0, queued_fleet=[])),
                )
                output, calls = self.pick(
                    fixtures, config=config, FLEET_JOB_KIND="staging", FLEET_CI_ENABLED="true",
                    FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3", GITHUB_STATE_TOKEN="state",
                    GITHUB_RUN_ATTEMPT="2",
                )
                self.assertEqual(output["pool"], expected)
                self.assertEqual(output["attempt"], "2")
                self.assertIn(("fleet", "orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1"), calls)
                self.assertFalse(any("orgs/Test/repos" in path for _, path in calls))

    def test_staging_busy_offline_mislabeled_or_unavailable_inventory_retains_cloud_fallback(self):
        config, inventory = self.staging_fixture()
        for failure in ("busy", "offline", "labels", "unavailable"):
            current = json.loads(json.dumps(inventory))
            for guest in current["runners"]:
                if failure == "busy": guest["busy"] = True
                if failure == "offline": guest["status"] = "offline"
                if failure == "labels": guest["labels"] = []
            if failure == "unavailable": current = OSError("inventory offline")
            output, calls = self.pick(dict(fleet={"orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1": current}, state=dict(shared=self.shared(routing_version=2, namespace_busy=0, queued_fleet=[]))), config=config, FLEET_JOB_KIND="staging", FLEET_CI_ENABLED="true", FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3", GITHUB_STATE_TOKEN="state")
            self.assertEqual(output["pool"], "namespace", failure)

    def shared(self, **changes):
        now = time.time() * 1000
        value = dict(complete=True, organization="Test", busy=0, observed_at=now, retry_at=now + 30000)
        value.update(changes)
        return value

    def test_fresh_existing_shared_snapshot_avoids_direct_github_reads(self):
        output, calls = self.pick(dict(state=dict(shared=self.shared())), GITHUB_STATE_TOKEN="state")
        self.assertEqual(output["pool"], "github")
        self.assertEqual(calls, [("state", "shared")])

    def test_unexpired_two_minute_shared_snapshot_avoids_direct_reads(self):
        snapshot=self.shared(observed_at=time.time()*1000-110000)
        output,calls=self.pick(dict(state=dict(shared=snapshot)),GITHUB_STATE_TOKEN="state")
        self.assertEqual(output["pool"],"github")
        self.assertEqual(calls,[("state","shared")])

    def test_v2_snapshot_enforces_namespace_cap_without_direct_discovery(self):
        snapshot = self.shared(routing_version=2, namespace_busy=20, queued_fleet=[])
        output, calls = self.pick(dict(state=dict(shared=snapshot)), GITHUB_STATE_TOKEN="state", FLEET_JOB_KIND="staging")
        self.assertEqual(output["pool"], "github")
        self.assertEqual(output["namespace_busy"], "20")
        self.assertEqual(calls, [("state", "shared")])

    def test_partial_shared_capacity_never_asserts_namespace_spare_capacity(self):
        for kind, busy in [("pr", 20), ("staging", 0), ("other", 20)]:
            for lower, pool in [(0, "github"), (16, "github"), (20, "github"), (None, "github")]:
                with self.subTest(kind=kind, lower=lower):
                    value = self.shared(busy=busy, namespace_busy_lower_bound=lower)
                    output, calls = self.pick(dict(state=dict(shared=value)), GITHUB_STATE_TOKEN="state", BUDGET="12", FLEET_JOB_KIND=kind)
                    self.assertEqual(output["pool"], pool)
                    self.assertEqual(output["namespace_busy"], "")
                    self.assertEqual(calls, [("state", "shared")])
                    self.assertIn("inventory=partial" if lower is not None else "inventory=unavailable", self.last_log)
                    self.assertIn("cap reached" if lower == 20 else "capacity unknown", self.last_log)
                    self.assertIn(f">={lower}/20" if lower is not None else "unknown/20", self.last_log)

        for lower in [True, "0", -1, 10001]:
            with self.subTest(invalid_lower=lower):
                output, calls = self.pick(dict(state=dict(shared=self.shared(busy=20, namespace_busy_lower_bound=lower))), GITHUB_STATE_TOKEN="state")
                self.assertEqual(output["pool"], "github")
                self.assertEqual(output["busy"], "20")
                self.assertEqual(calls, [("state", "shared")])

    def test_complete_namespace_count_below_configured_cap_keeps_preference_order(self):
        for count in (0, 16, 19):
            snapshot = self.shared(busy=20, routing_version=2, namespace_busy=count, queued_fleet=[])
            output, calls = self.pick(dict(state=dict(shared=snapshot)), GITHUB_STATE_TOKEN="state", BUDGET="12")
            self.assertEqual(output["pool"], "namespace")
            self.assertIn(f"{count}/20; inventory=complete; capacity available", self.last_log)
            self.assertEqual(calls, [("state", "shared")])

    def test_saturated_lower_bound_reports_reason_and_keeps_later_idle_machine(self):
        config, inventory = self.staging_fixture()
        config = config.replace('[services.fleet]\n', '[services.fleet]\npr_order=["namespace","chris","github"]\n')
        snapshot = self.shared(busy=20, namespace_busy_lower_bound=22, placement_unavailable="capacity_namespace_target")
        output, calls = self.pick(dict(state=dict(shared=snapshot),
            fleet={"orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1": inventory}),
            config=config, FLEET_CI_ENABLED="true", FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3", GITHUB_STATE_TOKEN="state")
        self.assertEqual(output["pool"], "chris")
        self.assertEqual(output["namespace_busy_lower_bound"], "22")
        self.assertEqual(output["placement_unavailable"], "capacity_namespace_target")
        self.assertIn(">=22/20; inventory=partial; cap reached; reason=capacity_namespace_target", self.last_log)
        self.assertFalse(any(token in ("app", "repository") for token, _ in calls))

    def test_requested_cap_is_used_for_discovery_and_admission(self):
        for cap, count, expected in [(8, 7, "namespace"), (8, 8, "github"), (30, 20, "namespace"), (30, 30, "github")]:
            snapshot = self.shared(busy=20, routing_version=2, namespace_busy=count, queued_fleet=[])
            output, _ = self.pick(dict(state=dict(shared=snapshot)), GITHUB_STATE_TOKEN="state", BUDGET="12", NAMESPACE_BUILD_CAP=str(cap))
            self.assertEqual(output["pool"], expected)
            self.assertIn(f"namespace_cap={cap}", self.last_shared_urls[0])
            self.assertIn(f"{count}/{cap}; inventory=complete", self.last_log)

    def test_partial_fleet_reservations_exclude_observed_targets(self):
        config, inventory = self.staging_fixture()
        queued = [[v["name"] for v in inventory["runners"][1]["labels"]]]
        snapshot = self.shared(namespace_busy_lower_bound=20, queued_fleet=queued)
        output, _ = self.pick(dict(state=dict(shared=snapshot),
            fleet={"orgs/Test/actions/runner-groups/3/runners?per_page=100&page=1": inventory}), config=config,
            FLEET_JOB_KIND="staging", FLEET_CI_ENABLED="true", FLEET_READ_TOKEN="fleet", FLEET_RUNNER_GROUP_ID="3", GITHUB_STATE_TOKEN="state")
        self.assertEqual(output["pool"], "chris")

    def test_partial_reason_cannot_inject_workflow_outputs(self):
        for reason in ["capacity_scan_limit\nrunner=wrong", "::warning::bad", {"message": "failure"}, 1, "capacity_" + "x" * 65]:
            snapshot = self.shared(namespace_busy_lower_bound=20, placement_unavailable=reason)
            output, _ = self.pick(dict(state=dict(shared=snapshot)), GITHUB_STATE_TOKEN="state", BUDGET="12")
            self.assertEqual(output["pool"], "github")
            self.assertEqual(output["placement_unavailable"], "")
            self.assertNotIn("reason=", self.last_log)
            self.assertNotEqual(output["runner"], "wrong")

    def test_invalid_shared_values_keep_native_fallback_without_duplicate_scans(self):
        for changes in [dict(observed_at=0), dict(observed_at=time.time()*1000-160000), dict(organization="Other"), dict(busy=True),
                        dict(observed_at=float("nan")), dict(retry_at=0), dict(routing_version=2, namespace_busy=-1, queued_fleet=[])]:
            with self.subTest(changes=changes):
                output, calls = self.pick(dict(state=dict(shared=self.shared(**changes)), app=self.fixture()), GITHUB_STATE_TOKEN="state")
                self.assertEqual(output["pool"], "github")
                self.assertEqual(calls, [("state", "shared")])

    def test_real_api_link_pagination_accepts_live_actions_count_drift(self):
        fixtures = self.fixture()
        path = "repos/Test/code/actions/runs/3/jobs?per_page=100&page=1"
        fixtures[path]["total_count"] = 99
        output, _ = self.pick(dict(app=fixtures))
        self.assertEqual(output["pool"], "github")
        self.assertEqual(output["busy"], "0")

    def test_low_shared_quota_restarts_complete_scan_using_repository_budget(self):
        output, calls = self.pick(dict(app=self.fixture(remaining=1500), repository=self.fixture()))
        self.assertEqual(output["pool"], "github")
        self.assertEqual(calls[0], ("app", "rate_limit"))
        self.assertFalse(any(token == "app" for token, path in calls[1:]))
        self.assertIn(("repository", "orgs/Test/repos?per_page=100&page=1"), calls)

    def test_partial_app_scan_is_discarded_and_repository_snapshot_wins(self):
        app = self.fixture(busy=0)
        app["repos/Test/code/actions/runs/3/jobs?per_page=100&page=1"] = OSError("unavailable")
        output, calls = self.pick(dict(app=app, repository=self.fixture(busy=19)))
        self.assertEqual(output["pool"], "namespace")
        self.assertEqual(output["busy"], "19")
        self.assertEqual(output["namespace_cap"], "20")
        self.assertIn(("repository", "orgs/Test/repos?per_page=100&page=1"), calls)

    def test_two_exhausted_budgets_wait_on_github_without_bypassing_namespace_cap(self):
        output, calls = self.pick(dict(app=self.fixture(remaining=1500), repository=self.fixture(remaining=250)))
        self.assertEqual(output["pool"], "github")
        self.assertEqual(json.loads(output["runner"]), "ubuntu-24.04")
        self.assertEqual(calls, [("app", "rate_limit"), ("repository", "rate_limit")])

    def test_mid_scan_requests_stop_at_quota_reserve(self):
        output, calls = self.pick(dict(app=self.fixture(remaining=1501), repository=self.fixture(remaining=251)))
        self.assertEqual(output["pool"], "github")
        self.assertFalse(any("actions/runs" in path for token, path in calls))

    def test_namespace_staging_shape_priority_assignment_and_attempt_are_preserved(self):
        output, _ = self.pick(dict(app=self.fixture()), FLEET_JOB_KIND="staging")
        self.assertEqual(output["pool"], "namespace")
        self.assertEqual(output["attempt"], "1")
        self.assertEqual(json.loads(output["runner"]), ["nscloud-ubuntu-24.04-amd64-16x32-with-features;job.priority=1", "namespace-features:container.privileged=true;github.run-id=5"])


class Recovery(unittest.TestCase):
    def advance(self, seconds):
        self.now += seconds

    def test_policy_recovery_preserves_run_identity_on_a_later_staging_attempt(self):
        fetch = self.fixture()
        self.run["run_attempt"] = 2
        self.job["labels"] = ["self-hosted", "tauceti-fleet-staging", "fleet-" + "b" * 32]
        result = recover(42, fetch=fetch, clock=lambda: self.now, sleep=self.advance, policy=True)
        self.assertEqual(result, "rerun-with-updated-placement-requested")
        self.assertTrue(any("/attempts/2/jobs?" in path for path, _ in self.calls))
        self.assertEqual([path for path, data in self.calls if data is not None], ["/actions/runs/42/force-cancel", "/actions/runs/42/rerun"])

    def test_pr_pool_waiting_never_enters_startup_cancellation_or_rerun(self):
        for event in ("pull_request_target", "repository_dispatch"):
            fetch = self.fixture()
            self.run.update(event=event, display_title="pr-build pr=7 head=" + "a" * 40)
            self.job["labels"] = ["self-hosted", "tauceti-fleet-pr"]
            self.assertEqual(recover(42, fetch=fetch, policy=True), "not-an-initial-bors-attempt")
            self.assertEqual(self.calls, [("/actions/runs/42", None)])
            self.assertFalse(self.canceled)

    def test_slow_picker_gets_full_acquisition_window_after_labels_appear(self):
        fetch = self.fixture()
        when = []
        def delayed(path, data=None):
            if path.endswith("/force-cancel"):
                when.append(self.now)
            result = fetch(path, data)
            if "/jobs?" in path and self.now < 1400:
                result["jobs"] = [dict(self.job, labels=[])]
            return result
        self.assertEqual(recover(42, fetch=delayed, clock=lambda: self.now, sleep=self.advance),
                         "rerun-on-namespace-requested")
        self.assertEqual(when, [1520])

    def test_transient_read_failure_is_retried_without_abandoning_recovery(self):
        fetch = self.fixture()
        failed = False
        def transient(path, data=None):
            nonlocal failed
            if "/jobs?" in path and not failed:
                failed = True
                raise OSError("temporary 502")
            return fetch(path, data)
        self.assertEqual(recover(42, fetch=transient, clock=lambda: self.now, sleep=self.advance),
                         "rerun-on-namespace-requested")
        self.assertTrue(failed)

    def test_definite_force_cancel_rejection_uses_ordinary_cancellation(self):
        fetch = self.fixture()
        def rejected(path, data=None):
            if path.endswith("/force-cancel"):
                raise urllib.error.HTTPError(path, 403, "forbidden", {}, None)
            if path.endswith("/cancel"):
                self.canceled = True
                self.calls.append((path, data))
                return {}
            return fetch(path, data)
        self.assertEqual(recover(42, fetch=rejected, clock=lambda: self.now, sleep=self.advance),
                         "rerun-on-namespace-requested")
        self.assertTrue(any(p.endswith("/cancel") for p, d in self.calls))

    def fixture(self, *, acquired=False):
        self.now = 1000
        self.calls = []
        self.canceled = False
        self.run = dict(
            id=42,
            event="repository_dispatch",
            run_attempt=1,
            status="in_progress",
            display_title="bors branch=main batch=1 head="
            + "a" * 40
            + " base="
            + "b" * 40,
            path=".github/workflows/pr-build.yml",
            repository=dict(full_name="TauCetiProject/TauCeti"),
        )
        self.job = dict(
            name="sandboxed-build",
            labels=["self-hosted", "tauceti-fleet"],
            status="queued",
            runner_name="runner" if acquired else "",
            created_at="1970-01-01T00:00:00Z",
        )

        def fetch(path, data=None):
            self.calls.append((path, data))
            if path.endswith("/force-cancel"):
                self.canceled = True
                return {}
            if path.endswith("/rerun"):
                return {}
            if "/jobs?" in path:
                return dict(total_count=1, jobs=[self.job])
            return dict(
                self.run,
                **(
                    dict(status="completed", conclusion="cancelled")
                    if self.canceled
                    else {}
                ),
            )

        return fetch

    def test_two_minute_wait_reuses_same_run_and_does_not_redispatch(self):
        fetch = self.fixture()
        result = recover(
            42,
            fetch=fetch,
            clock=lambda: self.now,
            sleep=self.advance,
        )
        self.assertEqual(result, "rerun-on-namespace-requested")
        self.assertEqual(
            [p for p, d in self.calls if d is not None],
            ["/actions/runs/42/force-cancel", "/actions/runs/42/rerun"],
        )

    def test_empty_labels_wait_for_placement_and_unobservable_picker_is_error(self):
        fetch = self.fixture()
        self.job["labels"] = []
        def sleep(seconds):
            self.now += seconds
        with self.assertRaisesRegex(TimeoutError, "placement"):
            recover(42, fetch=fetch, clock=lambda: self.now, sleep=sleep)
        self.assertFalse(self.canceled)

    def test_rerun_failure_posts_terminal_failure_instead_of_leaving_bors_pending(self):
        fetch = self.fixture()
        def failing(path, data=None):
            if path.endswith("/rerun"):
                raise OSError("lost response")
            if "/statuses/" in path:
                self.calls.append((path, data))
                return {}
            return fetch(path, data)
        def sleep(seconds):
            self.now += seconds
        with self.assertRaisesRegex(RuntimeError, "not confirmed"):
            recover(42, fetch=failing, clock=lambda: self.now, sleep=sleep)
        posts = [(p, d) for p, d in self.calls if "/statuses/" in p]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1]["state"], "failure")

    def test_lost_rerun_response_is_reconciled_without_repeating_mutation(self):
        fetch = self.fixture()
        def lost(path, data=None):
            if path.endswith("/rerun"):
                self.run["run_attempt"] = 2
                raise OSError("lost response")
            return fetch(path, data)
        self.assertEqual(recover(42, fetch=lost, clock=lambda: self.now, sleep=self.advance), "rerun-on-namespace-observed")
        self.assertFalse(any("/statuses/" in p for p, d in self.calls))

    def test_cancellation_timeout_posts_failure_and_does_not_start_overlapping_retry(self):
        fetch = self.fixture()
        def stalled(path, data=None):
            if path.endswith("/force-cancel") or "/statuses/" in path:
                self.calls.append((path, data))
                return {}
            return fetch(path, data)
        def sleep(seconds):
            self.now += seconds
        with self.assertRaisesRegex(TimeoutError, "cancellation"):
            recover(42, fetch=stalled, clock=lambda: self.now, sleep=sleep)
        self.assertFalse(any(p.endswith("/rerun") for p, d in self.calls))
        self.assertTrue(any("/statuses/" in p for p, d in self.calls))

    def test_acquisition_during_force_cancel_reports_infrastructure_failure(self):
        fetch = self.fixture()
        def raced(path, data=None):
            if path.endswith("/force-cancel"):
                self.job["runner_id"] = 12
            return fetch(path, data)
        self.assertEqual(recover(42, fetch=raced, clock=lambda: self.now, sleep=self.advance), "runner-acquired-during-cancellation")
        self.assertFalse(any(p.endswith("/rerun") for p, d in self.calls))
        self.assertTrue(any("/statuses/" in p for p, d in self.calls))

    def test_acquired_runner_is_never_canceled(self):
        fetch = self.fixture(acquired=True)
        self.assertEqual(recover(42, fetch=fetch), "runner-acquired")
        self.assertFalse(any(d is not None for _, d in self.calls))

    def test_second_attempt_has_no_fallback_loop(self):
        fetch = self.fixture()
        self.run["run_attempt"] = 2
        self.assertEqual(recover(42, fetch=fetch), "not-an-initial-bors-attempt")

    def test_truncated_jobs_cannot_authorize_cancellation(self):
        fetch = self.fixture()

        def incomplete(path, data=None):
            value = fetch(path, data)
            if "/jobs?" in path:
                value["total_count"] = 2
            return value

        with self.assertRaises(ValueError):
            recover(42, fetch=incomplete)
        self.assertFalse(self.canceled)


if __name__ == "__main__":
    unittest.main()
