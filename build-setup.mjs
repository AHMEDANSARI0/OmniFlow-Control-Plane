// Builds /home/user/omniflow_setup.mjs from the VERIFIED files in this workspace.
import fs from "node:fs";
import path from "node:path";
const REPO = "/home/user/Omniflow";
const PATCH = "/home/user/omniflow-backend-patch";
const OUT = "/home/user/omniflow_setup.mjs";
const WEBSITE_PATHS = [
  "app/dashboard/login/page.tsx",
  "app/dashboard/forgot-password/page.tsx",
  "app/dashboard/reset-password/page.tsx",
  "app/api/omniflow/auth/forgot-password/route.ts",
  "app/api/omniflow/auth/reset-password/route.ts",
  "app/api/omniflow/admin/clients/route.ts",
  "app/api/omniflow/admin/clients/[id]/reset-password/route.ts",
  "app/api/omniflow/admin/reset-codes/route.ts",
  "app/admin/(panel)/customers/page.tsx",
  "lib/omniflow/forgot-password.ts",
  "lib/omniflow/admin-control-plane.ts",
  "lib/omniflow/portal.ts",
  "app/api/omniflow/portal/channels/whatsapp/route.ts",
  "app/api/omniflow/portal/bot/route.ts",
  "app/dashboard/(portal)/channels/whatsapp/page.tsx",
  "app/dashboard/(portal)/bot/page.tsx",
  "app/dashboard/(portal)/bot/BotForm.tsx",
  "docs/PORTAL_API.md",
];
const BACKEND_PATHS = [
  "auth_password_reset.py",
  "admin_users.py",
  "migrations/007_password_reset_tokens.sql",
];
function b64map(paths, baseDir) {
  const out = {};
  for (const p of paths) {
    const full = path.join(baseDir, p);
    if (!fs.existsSync(full)) throw new Error("missing source file: " + full);
    out[p] = fs.readFileSync(full).toString("base64");
  }
  return out;
}
function entriesLiteral(map) {
  const lines = Object.entries(map).map(
    ([k, v]) => "  " + JSON.stringify(k) + ": " + JSON.stringify(v) + ","
  );
  return "{\n" + lines.join("\n") + "\n}";
}
const HEADER = `#!/usr/bin/env node
/**
 * OMNIFLOW — setup/patch file (single file, zero arguments).
 *
 *         node omniflow_setup.mjs
 *
 * Kis repo mein chalao:
 *   - WEBSITE repo root (jahan package.json hai)    -> website files + sidebar + proxy patch
 *   - BACKEND repo root (jahan app.py hai)          -> backend files (+ app.py patch agar zaroori)
 *
 * Agar website aur backend dono repos aik hi folder ke ANDAR hain, to sirf
 * aik baar chalao — script doosri repo khud dhoondh kar patch kar degi.
 *
 * Optional:  node omniflow_setup.mjs --push   (commit + push bhi)
 */`;
const HARNESS = `
const root = process.cwd();
const doPush = process.argv.slice(2).includes("--push");
const CYAN = "\\x1b[36m";
const GREEN = "\\x1b[32m";
const YELLOW = "\\x1b[33m";
const RED = "\\x1b[31m";
const BOLD = "\\x1b[1m";
const OFF = "\\x1b[0m";
function info(msg) { console.log(CYAN + "==> " + OFF + msg); }
function ok(msg)   { console.log("  " + GREEN + "+" + OFF + " " + msg); }
function warn(msg) { console.log("  " + YELLOW + "!" + OFF + " " + msg); }
function fail(msg) { console.error(RED + "ERROR: " + OFF + msg); process.exit(1); }
function detectType(dir) {
  if (fs.existsSync(path.join(dir, "app.py"))) return "backend";
  if (fs.existsSync(path.join(dir, "package.json"))) return "website";
  return null;
}
function findSiblingOf(otherType) {
  const parent = path.dirname(root);
  let hits = [];
  try {
    for (const name of fs.readdirSync(parent)) {
      const p = path.join(parent, name);
      if (p === root) continue;
      let st;
      try { st = fs.statSync(p); } catch { continue; }
      if (!st.isDirectory()) continue;
      if (detectType(p) === otherType) hits.push(p);
    }
  } catch { /* parent not readable */ }
  return hits.length === 1 ? hits[0] : null;
}
function writeFiles(map, label, baseDir) {
  let written = 0;
  let upToDate = 0;
  for (const [rel, b64] of Object.entries(map)) {
    const dest = path.join(baseDir, ...rel.split("/"));
    const content = Buffer.from(b64, "base64");
    if (fs.existsSync(dest)) {
      try {
        if (fs.readFileSync(dest).equals(content)) { upToDate++; continue; }
      } catch { /* fall through and overwrite */ }
    }
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    fs.writeFileSync(dest, content);
    written++;
    ok(label + " " + rel);
  }
  console.log("  (" + written + " written, " + upToDate + " already up to date)\\n");
}
function patchSidebar(dir) {
  const file = path.join(dir, "app", "admin", "components", "AdminSidebar.tsx");
  if (!fs.existsSync(file)) { warn("AdminSidebar.tsx nahi mila (skip)"); return; }
  let s = fs.readFileSync(file, "utf8");
  if (s.includes('href: "/admin/customers"')) { ok("AdminSidebar: Customers link already present (skip)"); return; }
  const anchor = '{ label: "Leads", href: "/admin/leads", icon: "◇", enabled: true },';
  if (!s.includes(anchor)) { warn("AdminSidebar anchor nahi mila — Customers link manually add karein"); return; }
  s = s.replace(anchor, anchor + '\\n  { label: "Customers", href: "/admin/customers", icon: "◉", enabled: true },', 1);
  fs.writeFileSync(file, s);
  ok("AdminSidebar: Customers link add ho gaya");
}
function patchProxy(dir) {
  const file = path.join(dir, "proxy.ts");
  if (!fs.existsSync(file)) { warn("proxy.ts nahi mila (skip)"); return; }
  let s = fs.readFileSync(file, "utf8");
  if (s.includes('"/dashboard/forgot-password"')) { ok("proxy.ts: forgot/reset allowlist already present (skip)"); return; }
  const anchor = 'pathname === "/dashboard/unavailable"';
  if (!s.includes(anchor)) { warn("proxy.ts anchor nahi mila — forgot/reset routes manually allowlist karein"); return; }
  s = s.replace(
    anchor,
    anchor +
      ' ||\\n      pathname === "/dashboard/forgot-password" ||' +
      '\\n      pathname === "/dashboard/reset-password"',
    1
  );
  fs.writeFileSync(file, s);
  ok("proxy.ts: forgot/reset-password allowlist me (pre-auth access)");
}
function patchAppPy(dir) {
  const file = path.join(dir, "app.py");
  if (!fs.existsSync(file)) { warn("app.py nahi mila (skip)"); return; }
  let s = fs.readFileSync(file, "utf8");
  const hasImports = s.includes("from auth_password_reset import") && s.includes("from admin_users import");
  const hasRegister = s.includes("register_blueprint(auth_password_reset_bp)");
  if (hasImports && hasRegister) { ok("app.py already patched (skip)"); return; }
  warn("app.py patch nahi hua — is deployment me composite WSGI entry point chahiye.");
  console.log("     (Aap ke repo me already fixed version hai; agar nahi hai to handoff wala app_fixed.py use karein)");
}
function gitCommitPush(dir, label) {
  if (!fs.existsSync(path.join(dir, ".git"))) { warn(label + " git repo nahi hai (skip push)"); return; }
  try {
    const branch = execFileSync("git", ["branch", "--show-current"], { cwd: dir, encoding: "utf8" }).trim();
    execFileSync("git", ["add", "-A"], { cwd: dir });
    execFileSync("git", ["commit", "-m", "feat: portal modules + admin reset codes panel + fixes"], { cwd: dir });
    execFileSync("git", ["push", "origin", branch], { cwd: dir });
    ok(label + ": commit + push ho gaya (" + branch + ")");
  } catch {
    warn(label + ": commit/push fail — khud karein: git add -A && git commit && git push");
  }
}
function applyTo(type, dir) {
  info("Repo mila: " + BOLD + type + OFF + "  ->  " + dir);
  console.log("");
  if (type === "website") {
    writeFiles(WEBSITE_FILES, "[website]", dir);
    patchSidebar(dir);
    patchProxy(dir);
  } else {
    writeFiles(BACKEND_FILES, "[backend]", dir);
    patchAppPy(dir);
  }
}
const selfType = detectType(root);
if (!selfType) {
  fail("Yeh folder koi OmniFlow repo nahi hai.\\n" +
       "   Website root: jahan package.json hai   |   Backend root: jahan app.py hai\\n" +
       "   Script file ko project root mein rakho aur 'node <script>.mjs' chalao.");
}
const touched = [];
applyTo(selfType, root);
touched.push({ dir: root, type: selfType });
const otherType = selfType === "website" ? "backend" : "website";
const sibling = findSiblingOf(otherType);
if (sibling) {
  console.log(CYAN + "==> " + OFF + "Doosri repo mil gayi (aik hi folder me): " + BOLD + sibling + OFF + "\\n");
  applyTo(otherType, sibling);
  touched.push({ dir: sibling, type: otherType });
} else {
  console.log(CYAN + "==> " + OFF + "Doosri repo is folder ke saath nahi mili (us repo me bhi chala sakte ho).\\n");
}
if (doPush) {
  console.log("");
  for (const t of touched) gitCommitPush(t.dir, "[" + t.type + "]");
}
console.log("\\n" + GREEN + "============================================================" + OFF);
console.log("  Sab changes apply ho gaye!");
console.log("  - Backend repo: commit + push karo (Vercel auto-deploy)");
console.log("  - Website repo: commit + push karo (ya --push use kiya tha)");
console.log("  - Admin panel > Customers > 'Recent password reset codes' panel");
console.log(GREEN + "============================================================" + OFF);
`;
const websiteFiles = b64map(WEBSITE_PATHS, REPO);
const backendFiles = b64map(BACKEND_PATHS, PATCH);
const script =
  HEADER +
  "\nimport fs from \"node:fs\";\n" +
  "import path from \"node:path\";\n" +
  "import { execFileSync } from \"node:child_process\";\n\n" +
  "const WEBSITE_FILES = " + entriesLiteral(websiteFiles) + ";\n\n" +
  "const BACKEND_FILES = " + entriesLiteral(backendFiles) + ";\n" +
  HARNESS;
fs.writeFileSync(OUT, script);
console.log("written:", OUT, script.length, "bytes");

