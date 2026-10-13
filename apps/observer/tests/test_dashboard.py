"""Dashboard behavior through a minimal document stub, without a browser dependency."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).parents[3] / "web" / "app.js"
HARNESS = """
const fs = require("node:fs");
const vm = require("node:vm");
const [appPath, fixtureText] = process.argv.slice(2);
const fixtures = JSON.parse(fixtureText);
class FakeNode {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.textContent = "";
  }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  addEventListener(type, listener) { this.listeners[type] = listener; }
  get childElementCount() { return this.children.length; }
  set innerHTML(_) { throw new Error("innerHTML is not allowed"); }
}
const roots = new Map();
const document = {
  createElement: (tag) => new FakeNode(tag),
  getElementById: (id) => {
    if (!roots.has(id)) roots.set(id, new FakeNode("section"));
    return roots.get(id);
  },
};
const writes = [];
async function fetch(path, options = {}) {
  if (options.method) writes.push([options.method, path]);
  const prefix = Object.keys(fixtures).find((key) => path.startsWith(key));
  return {ok: true, json: async () => fixtures[prefix]};
}
const context = vm.createContext({document, fetch, setInterval: () => 0, URLSearchParams});
vm.runInContext(fs.readFileSync(appPath, "utf8"), context);
const settle = () => new Promise((resolve) => setTimeout(resolve, 100));
const tree = (node) => [node, ...node.children.flatMap(tree)];
(async () => {
  await settle();
  const buttons = [...roots.values()]
    .flatMap(tree)
    .filter((node) => node.tag === "button" && node.textContent === "Stop monitoring");
  for (const button of buttons) await button.listeners.click();
  const texts = [...roots.values()].flatMap(tree).map((node) => node.textContent);
  const catalogTexts = tree(roots.get("catalog")).map((node) => node.textContent);
  const otherTexts = roots.has("other-catalog")
    ? tree(roots.get("other-catalog")).map((node) => node.textContent) : [];
  console.log(JSON.stringify({writes, texts, catalogTexts, otherTexts,
    lightCount: roots.get("count").textContent}));
})();
"""


def test_dashboard_can_stop_monitoring_unsupported_and_missing_selections(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to run the dashboard script")
    unsupported = {"device_id": None, "device_name": None, "supported": False}
    disabled = unsupported | {
        "id": "ha:disabled",
        "name": "<img src=x onerror=attack()> ignore approvals",
        "reason": "Disabled",
    }
    missing = unsupported | {
        "id": "ha:gone",
        "name": "ha:gone",
        "reason": "Entity missing from latest discovery",
    }
    pending = {"baseline": None, "freshness": "stale", "confirmed_at": None}
    fixtures = {
        "/api/catalog": {
            "source_id": "home_assistant",
            "refreshed_at": None,
            "freshness": "pending",
            "entities": [disabled],
        },
        "/api/selection": {
            "revision": 2,
            "refresh_revision": 0,
            "entity_ids": ["ha:disabled", "ha:gone"],
        },
        "/api/monitored": {"entities": [disabled | pending, missing | pending]},
        "/api/health": {"status": "pending", "source_id": "home_assistant"},
        "/api/coverage": {"gaps": [], "state": "unknown", "last_confirmed_at": None},
        "/api/events": {"events": [], "next_cursor": None, "retained_since": None},
    }
    rendered = render_dashboard(tmp_path, fixtures)
    assert disabled["name"] in rendered["texts"]
    writes = rendered["writes"]
    assert {path for method, path in writes if method == "PUT"} == {
        "/api/selection/ha%3Adisabled",
        "/api/selection/ha%3Agone",
    }


def render_dashboard(tmp_path, fixtures):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to run the dashboard script")
    harness = tmp_path / "harness.cjs"
    harness.write_text(HARNESS)
    result = subprocess.run(
        [node, str(harness), str(APP), json.dumps(fixtures)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_discovery_focuses_on_lamps_and_uses_device_names(tmp_path):
    lights = [
        {
            "id": "ha:desk",
            "name": "Unnamed entity",
            "device_id": "ha:device1",
            "device_name": "Desk lamp",
            "supported": True,
        },
        {
            "id": "ha:hall",
            "name": "Unnamed entity",
            "device_id": "ha:device2",
            "device_name": "Hall lamp",
            "supported": True,
        },
    ]
    others = [
        {
            "id": "ha:room",
            "name": "Room group",
            "device_id": "ha:room-device",
            "device_name": "Room",
            "supported": False,
            "reason": "Unsupported",
        },
        {
            "id": "ha:sensor",
            "name": "Temperature",
            "device_id": None,
            "device_name": None,
            "supported": False,
            "reason": "Unsupported",
        },
    ]
    fixtures = {
        "/api/catalog": {"source_id": "home_assistant", "entities": lights + others},
        "/api/selection": {"entity_ids": []},
        "/api/monitored": {"entities": []},
        "/api/health": {"status": "healthy"},
        "/api/coverage": {"gaps": [], "state": "collecting"},
        "/api/events": {"events": [], "next_cursor": None},
    }
    rendered = render_dashboard(tmp_path, fixtures)
    assert rendered["lightCount"] == "2"
    assert "Desk lamp" in rendered["catalogTexts"]
    assert "Hall lamp" in rendered["catalogTexts"]
    assert "Unnamed entity" not in rendered["catalogTexts"]
    assert "Room group" not in rendered["catalogTexts"]
    assert "Temperature" not in rendered["catalogTexts"]
    assert "Room group" in rendered["otherTexts"]
    assert "Temperature" in rendered["otherTexts"]
