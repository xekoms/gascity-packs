import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const workflow = readFileSync(new URL("./workflows/claude.yml", import.meta.url), "utf8");
const script = workflow.match(/\/\/ BEGIN CLAUDE ROUTING\n([\s\S]*?)\/\/ END CLAUDE ROUTING/)[1]
  .split("\n").map(line => line.replace(/^ {12}/, "")).join("\n");

function classify(eventName, payload, actor = "owner") {
  const outputs = {};
  new Function("context", "core", script)(
    { eventName, payload, actor, repo: { owner: "owner" } },
    { setOutput: (key, value) => { outputs[key] = value; } },
  );
  return outputs;
}

function eventPayload(eventName, text, association = "OWNER") {
  if (eventName === "issues") return { action: "opened", issue: { title: "", body: text, author_association: association } };
  if (eventName === "pull_request_review") return { action: "submitted", review: { body: text, author_association: association } };
  return { action: "created", comment: { body: text, author_association: association } };
}

test("exact upstream mention boundaries choose one billing route", () => {
  const cases = [
    ["@claude task", "subscription"], ["@claude-api task", "api"],
    ["@CLAUDE-API! task", "api"], ["@Claude, task", "subscription"],
    ["@claude-api", "api"], ["@claude", "subscription"],
    ["@claude fix; see @claude-api-notes", "subscription"],
    ["@claude-api-notes", ""], ["@claude_extra", ""],
    ["prefix@claude-api", ""], ["(@claude-api)", ""],
    ["@claude-api/task", ""], ["@claude-api.", "api"],
    ["@claude task; @claude-api task", "api"], ["plain text", ""],
    ["@claude task; $(touch /tmp/nope)", "subscription"],
  ];
  for (const eventName of ["issues", "issue_comment", "pull_request_review_comment", "pull_request_review"]) {
    for (const [text, billing] of cases) {
      assert.equal(classify(eventName, eventPayload(eventName, text)).billing, billing, eventName + ": " + text);
      assert.equal(classify(eventName, eventPayload(eventName, text, "CONTRIBUTOR"), "outsider").should_run, "false");
    }
  }
});

test("issue title and body are both classified; a valid API mention wins", () => {
  for (const [title, body, billing] of [
    ["@claude task", "@claude-api task", "api"],
    ["@claude-api task", "@claude task", "api"],
    ["@claude task", "@claude-api-notes", "subscription"],
    ["@claude-api-notes", "@claude task", "subscription"],
  ]) {
    assert.equal(classify("issues", { action: "opened", issue: { title, body, author_association: "OWNER" } }).billing, billing);
  }
});

test("assignment authorizes the assigning owner, not the author or assignee", () => {
  for (const actor of ["owner", "outsider"]) {
    for (const author_association of ["OWNER", "CONTRIBUTOR"]) {
      for (const login of ["owner", "outsider", "claude[bot]", null]) {
        for (const body of ["@claude task", "@claude-api task"]) {
          const result = classify("issues", { action: "assigned", issue: { title: "", body, author_association }, assignee: { login } }, actor);
          assert.equal(result.should_run, String(actor === "owner"));
        }
      }
    }
  }
});

test("manual billing validates the owner, route and nonempty prompt", () => {
  for (const billing of ["api", "subscription", "invalid"]) {
    for (const prompt of ["Review the PR URL", "", "   "]) {
      for (const actor of ["owner", "outsider"]) {
        const result = classify("workflow_dispatch", { inputs: { billing, prompt } }, actor);
        assert.equal(result.should_run, String(actor === "owner" && billing !== "invalid" && prompt.trim() !== ""));
      }
    }
  }
});

test("unsupported actions and events do not invoke Claude", () => {
  for (const eventName of ["issues", "issue_comment", "pull_request_review_comment", "pull_request_review"]) {
    const payload = eventPayload(eventName, "@claude-api task");
    payload.action = "edited";
    assert.equal(classify(eventName, payload).should_run, "false");
  }
  assert.equal(classify("schedule", {}).should_run, "false");
  assert.equal(classify("repository_dispatch", {}).should_run, "false");
});

test("only the selected credential reaches the action, with no cross-route fallback", () => {
  function credential(name, billing, secrets) {
    const expression = workflow.match(new RegExp("          " + name + ": \\$\\{\\{ (.+) \\}\\}"))[1];
    return new Function("needs", "secrets", "return (" + expression + ")")({ route: { outputs: { billing } } }, secrets);
  }
  for (const billing of ["subscription", "api", ""]) {
    for (const present of [true, false]) {
      const secrets = { ANTHROPIC_API_KEY: present ? "api-sentinel" : "", CLAUDE_CODE_OAUTH_TOKEN: present ? "subscription-sentinel" : "" };
      const api = credential("anthropic_api_key", billing, secrets);
      const oauth = credential("claude_code_oauth_token", billing, secrets);
      assert.equal(Boolean(api), billing === "api" && present);
      assert.equal(Boolean(oauth), billing === "subscription" && present);
      assert.ok(!(api && oauth));
    }
  }
  const apiMissing = { ANTHROPIC_API_KEY: "", CLAUDE_CODE_OAUTH_TOKEN: "subscription-sentinel" };
  assert.equal(credential("anthropic_api_key", "api", apiMissing), "");
  assert.equal(credential("claude_code_oauth_token", "api", apiMissing), "");
});

test("classification is before every gated original step and treats payloads as data", () => {
  assert.ok(workflow.indexOf("id: route") < workflow.indexOf("name: Checkout"));
  const scripts = workflow.match(/\/\/ BEGIN CLAUDE ROUTING[\s\S]*?\/\/ END CLAUDE ROUTING/)[0];
  assert.ok(!scripts.includes("${{"));
  const steps = workflow.split("      - name:").slice(2);
  for (const step of steps) assert.ok(step.includes("if: needs.route.outputs.should_run == 'true'"));
  assert.ok(workflow.includes('ANTHROPIC_API_KEY: ""'));
  assert.ok(workflow.includes('CLAUDE_CODE_OAUTH_TOKEN: ""'));
});

test("exact admission precedes shared action-job concurrency", () => {
  assert.ok(!/^concurrency:/m.test(workflow));
  assert.ok(workflow.includes("    needs: route\n    if: needs.route.outputs.should_run == 'true'"));
  const routeJob = workflow.slice(workflow.indexOf("  route:\n"), workflow.indexOf("  claude:\n"));
  assert.ok(!routeJob.includes("concurrency:"));
  assert.ok(!routeJob.includes("secrets."));
});
