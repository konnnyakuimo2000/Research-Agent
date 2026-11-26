// つなぐ先（デスクトップアプリ）: 住所の形・確かめ方・だめなときの理由。ゲートウェイの住所を前に付けて呼ぶこと
import { describe, expect, it, vi } from "vitest";
import { apiUrl, connect, gatewayBase, normalizeGateway, withKey } from "./api.ts";

const reply = (status: number) => vi.fn(async () => ({ status, ok: status < 400 }) as Response);

describe("つなぐ先", () => {
  it("住所の形をそろえる", () => {
    expect(normalizeGateway(" http://localhost:8100/ ")).toBe("http://localhost:8100");
    expect(normalizeGateway("https://133.2.23.25/ui/")).toBe("https://133.2.23.25");
    expect(normalizeGateway("localhost:8100")).toBe("http://localhost:8100");
    expect(normalizeGateway("")).toBe("");
  });

  it("確かめてから覚え、以後はその住所の /api を鍵付きで呼ぶ", async () => {
    const f = reply(200);
    expect(await connect("https://133.2.23.25/", "k1", f)).toBe("https://133.2.23.25");
    expect(f).toHaveBeenCalledWith("https://133.2.23.25/api/agents", { headers: { Authorization: "Bearer k1" } });
    expect(gatewayBase()).toBe("https://133.2.23.25");
    expect(localStorage.getItem("gatewayUrl")).toBe("https://133.2.23.25");
    expect(apiUrl("/api/runs")).toBe("https://133.2.23.25/api/runs");
    expect(withKey("/api/runs/r1/events")).toBe("https://133.2.23.25/api/runs/r1/events?key=k1");
  });

  it("だめなときは理由を返し、覚えない", async () => {
    const before = gatewayBase();
    await expect(connect("http://x", "bad", reply(401))).rejects.toThrow("鍵が違います");
    await expect(connect("http://x", "", reply(403))).rejects.toThrow("AGENT_API_KEY");
    await expect(connect("http://x", "", vi.fn(async () => { throw new TypeError("fetch failed"); }))).rejects.toThrow("つながりません");
    await expect(connect("  ", "", reply(200))).rejects.toThrow("住所を入れてください");
    expect(gatewayBase()).toBe(before);
  });
});
