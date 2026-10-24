// Native fetch owns upstream TLS. The local service only tunnels opaque bytes.
import fs from "node:fs";

export function createNativePlugin(configPath) {
  return {
    id: "zen-native-rotation",
    async setup(ctx) {
      const config = JSON.parse(fs.readFileSync(configPath, "utf8"));
      const control = `http://127.0.0.1:${config.port}`;
      const localHeaders = { "x-zen-rotator-token": config.token };
      const pending = new WeakMap();
      const eligible = (e) => e.model.providerID === "opencode" && e.model.id.endsWith("-free");
      const call = async (path, data) => {
        const response = await fetch(control + path, {
          method: data === undefined ? "GET" : "POST",
          headers: { ...localHeaders, "content-type": "application/json" },
          ...(data === undefined ? {} : { body: JSON.stringify(data) }),
          signal: AbortSignal.timeout(5000),
        });
        if (!response.ok) throw new Error("Zen local control unavailable");
        return response.json();
      };
      await ctx.session.hook("http.request", async (event) => {
        if (!eligible(event)) return;
        const original = event.request;
        const url = new URL(original.url);
        if (url.protocol !== "https:" || url.hostname !== "opencode.ai" || url.port ||
            !/^\/(?:inference\/[a-z0-9-]+\/v1|zen\/v1)\/(?:chat\/completions|responses|messages)$/.test(url.pathname)) return;
        const plan = await call("/plan");
        const body = await original.clone().arrayBuffer();
        let response;
        let lastFailure = 0;
        let previous429 = false;
        for (const [route, key] of plan.pairs) {
          if (original.signal.aborted) throw original.signal.reason;
          const headers = new Headers(original.headers);
          headers.set("authorization", `Bearer ${config.keys[key]}`);
          for (const name of ["x-api-key", "x-org-id", "x-opencode-org-id", "host", "content-length", "proxy-authorization"]) headers.delete(name);
          const proxyURL = new URL(`http://127.0.0.1:${plan.tunnel_port}`);
          proxyURL.username = String(route);
          proxyURL.password = config.token;
          const proxy = proxyURL.toString();
          try {
            response = await fetch(original.url, {
              method: original.method, headers, body, proxy,
              redirect: "manual", signal: original.signal,
            });
          } catch (error) {
            if (original.signal.aborted) throw error;
            lastFailure = 0;
            await call("/report", { route, key, status: 0 });
            continue;
          }
          try {
            await call("/report", { route, key, status: response.status,
              retry_after: response.headers.get("retry-after"), rotation: previous429 });
          } catch (error) {
            await response.body?.cancel();
            throw error;
          }
          previous429 = response.status === 429;
          lastFailure = response.status;
          if (![429, 401, 403].includes(response.status)) break;
          await response.body?.cancel();
          response = undefined;
        }
        const status = !plan.pairs.length || lastFailure === 429 ? 429 : lastFailure || 502;
        const errorType = status === 429 ? "FreeUsageLimitError" : status === 502 ? "transport_error" : "authentication_error";
        response ??= new Response(JSON.stringify({ error: { type: errorType, message: "All configured Zen routes/accounts unavailable; try later." } }),
          { status, headers: { "content-type": "application/json", ...(status === 429 ? { "retry-after": "60" } : {}) } });
        const placeholder = new Request(control + "/complete", { method: "POST", headers: localHeaders, signal: original.signal });
        pending.set(placeholder, response);
        event.request = placeholder;
      });
      await ctx.session.hook("http.response", async (event) => {
        const response = pending.get(event.request);
        if (!response) return;
        pending.delete(event.request);
        await event.response.body?.cancel();
        event.response = response;
      });
    },
  };
}
