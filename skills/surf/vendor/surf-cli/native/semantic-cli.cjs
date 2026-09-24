const { openClientTransport } = require("./client-transport.cjs");
const { SEMANTIC_POLICY, filter, find, verify } = require("./semantic-core.cjs");
const { createJevEvaluator } = require("./semantic-provider.cjs");
const {
  clearStoredTypeSafeCredential,
  credentialStatus,
  resolveTypeSafeCredential,
  setTypeSafeCredentialFromInput,
} = require("./semantic-credentials.cjs");

const HELP = `Usage:
  surf semantic.find <goal> [--tab-id <id>] [--json]
  surf semantic.verify <outcome> [--tab-id <id>] [--json]
  surf semantic.filter <goal> [--top <1-12>] [--tab-id <id>] [--json]
  surf semantic auth set|status|clear

This v2.9 compatibility backport sends bounded page text to TypeSafe for read-only ranking and verification. semantic.act is unavailable until the host and extension support stale-identity guards.`;

function normalizeArgs(argv) {
  if (argv[0] !== "semantic" || !argv[1] || argv[1].startsWith("-")) return argv;
  if (argv[1] === "auth" && argv[2]) return [`semantic.auth.${argv[2]}`, ...argv.slice(3)];
  return argv[1] ? [`semantic.${argv[1]}`, ...argv.slice(2)] : argv;
}

function positiveInteger(value, flag) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1) throw new Error(`${flag} must be a positive integer`);
  return parsed;
}

function parseSemanticArgs(argv) {
  const args = normalizeArgs(argv);
  const command = args[0];
  if (!command?.startsWith("semantic")) return null;
  if (command === "semantic" || args.includes("--help") || args.includes("-h")) return { command: "help", json: args.includes("--json") };
  if (["semantic.auth.set", "semantic.auth.status", "semantic.auth.clear"].includes(command)) {
    if (args.slice(1).some((arg) => arg !== "--json")) throw new Error("semantic auth commands accept only --json");
    return { command, json: args.includes("--json") };
  }
  if (command === "semantic.act") throw new Error("semantic.act is unavailable in the v2.9 compatibility backport");
  if (!["semantic.find", "semantic.verify", "semantic.filter"].includes(command)) throw new Error(`unknown semantic command: ${command}`);
  const result = { command, json: false };
  const positionals = [];
  for (let i = 1; i < args.length; i++) {
    const arg = args[i];
    if (arg === "--json") result.json = true;
    else if (arg === "--tab-id" || arg === "--top") {
      const value = args[++i];
      if (!value || value.startsWith("--")) throw new Error(`${arg} requires a value`);
      if (arg === "--tab-id") result.tabId = positiveInteger(value, arg);
      else result.top = positiveInteger(value, arg);
    } else if (arg.startsWith("--")) throw new Error(`unknown semantic option: ${arg}`);
    else positionals.push(arg);
  }
  if (positionals.length !== 1 || !positionals[0].trim()) throw new Error(`${command} requires exactly one goal or outcome`);
  if (result.top && result.top > SEMANTIC_POLICY.limits.filterTop) throw new Error(`--top must not exceed ${SEMANTIC_POLICY.limits.filterTop}`);
  result.goal = positionals[0].trim();
  return result;
}

function unwrapPageText(response) {
  if (response?.error) throw new Error(response.error.message || "browser request failed");
  const text = response?.result?.content?.find((item) => item.type === "text")?.text;
  if (typeof text !== "string") throw new Error("browser returned no readable page text");
  return text;
}

function boundedState(pageText) {
  const bounded = pageText.slice(0, SEMANTIC_POLICY.limits.stateBytes - 2048);
  const chunks = [];
  for (let offset = 0; offset < bounded.length && chunks.length < SEMANTIC_POLICY.limits.chunks; offset += 1600) {
    const text = bounded.slice(offset, offset + 1600).trim();
    if (text) chunks.push({ id: `chunk-${chunks.length + 1}`, text, refs: [] });
  }
  return { origin: "surf://active-tab", title: "", readyState: "unknown", modals: [], candidates: [], chunks };
}

async function runReadOnlySemantic(options, { request, evaluate }) {
  const state = boundedState(unwrapPageText(await request("page.read", {})));
  if (options.command === "semantic.verify") return verify({ state, outcome: options.goal, evidence: state.chunks, evaluate });
  if (options.command === "semantic.filter") return filter({ state, goal: options.goal, chunks: state.chunks, top: options.top, evaluate });
  return find({ state, goal: options.goal, candidates: state.chunks, evaluate });
}

async function handleSemanticCli(argv, { endpoint, env = process.env, input = process.stdin, output = process.stderr, openTransport = openClientTransport } = {}) {
  const options = parseSemanticArgs(argv);
  if (!options) return { handled: false };
  if (options.command === "help") return { handled: true, value: HELP, raw: true };
  if (options.command === "semantic.auth.set") return { handled: true, value: await setTypeSafeCredentialFromInput({ input, output, env }), json: options.json };
  if (options.command === "semantic.auth.status") return { handled: true, value: credentialStatus(env), json: options.json };
  if (options.command === "semantic.auth.clear") return { handled: true, value: { cleared: clearStoredTypeSafeCredential(env), status: credentialStatus(env) }, json: options.json };
  const credential = resolveTypeSafeCredential(env);
  if (!credential) { const error = new Error("TypeSafe API key is not configured; run `surf semantic auth set` or set TYPESAFE_API_KEY"); error.code = "provider_not_configured"; throw error; }
  const transport = await openTransport(endpoint, { requestTimeoutMs: SEMANTIC_POLICY.limits.maxWallMs });
  let id = 0;
  try {
    const request = (tool, args) => transport.request({
      type: "tool_request",
      method: "execute_tool",
      params: { tool, args },
      id: `semantic-${++id}`,
      ...(options.tabId ? { tabId: options.tabId } : {}),
    }, SEMANTIC_POLICY.limits.maxWallMs);
    const value = await runReadOnlySemantic(options, { request, evaluate: createJevEvaluator({ apiKey: credential.apiKey, env }) });
    return { handled: true, value, json: options.json };
  } finally { await transport.close(); }
}

function formatSemanticOutput(result) {
  if (result.raw) return result.value;
  if (result.json) return JSON.stringify(result.value, null, 2);
  if (result.value?.source) return result.value.fingerprint ? `${result.value.source} (${result.value.fingerprint})` : result.value.source;
  if (result.value?.cleared !== undefined) return result.value.cleared ? "Shared TypeSafe credential cleared for all clients." : "No shared TypeSafe credential found.";
  return JSON.stringify(result.value, null, 2);
}

module.exports = { formatSemanticOutput, handleSemanticCli, normalizeArgs, parseSemanticArgs, runReadOnlySemantic };
