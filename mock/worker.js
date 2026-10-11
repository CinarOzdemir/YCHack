// Mock of Freya Bank's statement-debt voice services (contract of 2026-10-01).
// Serves the exact test-environment answers from data.json (customerNo -> services).
import DATA from "./data.json";

const PREFIX = "/voice/v1/";
const CORS = { "Access-Control-Allow-Origin": "*" };
const json = (body, status = 200) => Response.json(body, { status, headers: CORS });

// customerNo -> services, and masked card number -> customerNo
const CARD_OWNER = {};
for (const [cno, s] of Object.entries(DATA)) {
  for (const svc of ["cards.balance", "statement.week.get", "statement.week.decide", "statement.week.change"]) {
    for (const card of Object.keys(s[svc] || {})) CARD_OWNER[card] = cno;
  }
}

let n = 0;
function errorBody(httpStatus, code, type, message, rootCause) {
  const guid = "e0" + Date.now().toString(16).padStart(14, "0") + (n++).toString(16).padStart(16, "0");
  return json({ result: { guid, returnCode: code, messageText: message, code, message, type,
    details: { guid, rootCause, errorOrigin: "freya-mock" } } }, httpStatus);
}
const invalid = (field) => errorBody(400, "INVALID_ARGUMENTS_ERROR", "ERROR", `${field} cannot be empty`, "InvalidArguments");
const NOT_FOUND = {
  "cards.list": ["FB_IVR_CHANNEL_102", "Müşteriye ait geçerli kart bilgisi bulunamadı."],
  "cards.balance": ["FB_IVR_CHANNEL_120", "Kart bulunamadı."],
  "statement.week.get": ["FB_IVR_CHANNEL_121", "Kart bulunamadı."],
  "statement.pdf.send": ["FB_IVR_CHANNEL_107", "Pdf dokümanı bulunamadı"],
};
function notFound(svc) {
  const nf = NOT_FOUND[svc];
  if (nf) return errorBody(404, nf[0], "WARNING", nf[1], "NotFound");
  return errorBody(400, "INVALID_ARGUMENTS_ERROR", "ERROR", "record not found", "InvalidArguments");
}

const isTemplate = (v) => typeof v === "string" && (v.includes("{{") || v.includes("%7B%7B"));
const str = (v) => (v === undefined || v === null ? "" : String(v).trim());

async function answer(entry) {
  if (!entry) return null;
  if (entry.delaySeconds) await new Promise((r) => setTimeout(r, entry.delaySeconds * 1000));
  return json(entry.response, entry.http);
}

async function handle(svc, body) {
  const d = (body && typeof body.data === "object" && body.data) || {};
  const first = Object.keys(DATA)[0];
  let cno = str(d.customerNo), card = str(d.cardNumber), week = str(d.newStatementWeek);
  if (isTemplate(cno)) cno = first;
  switch (svc) {
    case "cards.list":
    case "contact.email.get":
    case "statement.pdf.send": {
      if (!cno) return invalid("customerNo");
      if (svc === "contact.email.get" && !str(d.intAddressInfoType)) return invalid("intAddressInfoType");
      if (svc === "statement.pdf.send" && !card) return invalid("card number");
      const s = DATA[cno];
      return (s && (await answer(s[svc]))) || notFound(svc);
    }
    case "cards.balance":
    case "statement.week.get":
    case "statement.week.decide":
    case "statement.week.change": {
      if (!card) return invalid("card number");
      const owner = CARD_OWNER[card];
      const bySvc = owner && DATA[owner][svc];
      let entry = bySvc && bySvc[card];
      if (svc.startsWith("statement.week.d") || svc === "statement.week.change") {
        if (!week) return invalid("newStatementWeek");
        entry = entry && entry[week];
      }
      return (await answer(entry)) || notFound(svc);
    }
  }
  return json({ error: "unknown service" }, 404);
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return new Response(null, { headers: { ...CORS, "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "*" } });
    if (url.pathname === "/health") return json({ status: "ok", customers: Object.keys(DATA).length });
    if (request.method === "POST" && url.pathname === "/oauth2/token") return json({ access_token: "mock-token", token_type: "Bearer", expires_in: 1800 });
    if (request.method === "POST" && url.pathname.startsWith(PREFIX)) {
      let body = null;
      try { body = await request.json(); } catch {}
      return handle(url.pathname.slice(PREFIX.length), body);
    }
    return json({ error: "not found", paths: ["GET /health", "POST /oauth2/token", "POST /voice/v1/<service>"] }, 404);
  },
};
