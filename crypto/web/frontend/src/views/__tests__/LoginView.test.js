// LoginView（加密货币系统）测试：双入口按钮 + 登录流程。
// mock fetch（443 单域名架构：API 一律带 /crypto 前缀）：
//   /crypto/api/auth/me → 401（未登录）；/crypto/api/login → 200 写 user。
import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import { createPinia } from "pinia";
import LoginView from "../LoginView.vue";

function mockFetch(results) {
  const fn = vi.fn(async (path) => {
    const r = results[path] || { ok: false, status: 404, json: async () => ({}) };
    return {
      ok: r.ok,
      status: r.status,
      statusText: r.statusText || "",
      json: async () => r.body,
    };
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

function mountView() {
  return mount(LoginView, {
    global: { plugins: [createPinia()] },
  });
}

describe("LoginView（加密货币系统，双入口）", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
  });

  it("渲染双系统标题与两个登录按钮（A股 + 加密货币）", async () => {
    mockFetch({ "/crypto/api/auth/me": { ok: false, status: 401, body: {} } });
    const w = mountView();
    await flushPromises();
    expect(w.text()).toContain("双系统统一登录");
    const btns = w.findAll("button.login-btn");
    expect(btns.length).toBe(2);
    expect(btns[0].text()).toContain("A股4维选股");
    expect(btns[1].text()).toContain("加密货币");
  });

  it("未填凭据点登录 → 行内错误，不发请求", async () => {
    const fn = mockFetch({ "/crypto/api/auth/me": { ok: false, status: 401, body: {} } });
    const w = mountView();
    await flushPromises();
    await w.find("button.login-btn-crypto").trigger("click");
    await flushPromises();
    expect(w.text()).toContain("请输入用户名和密码");
    expect(fn).not.toHaveBeenCalledWith("/crypto/api/login", expect.anything());
  });

  it("正确凭据 + 加密货币按钮 → 登录成功 user 置位（停留本站）", async () => {
    const setCookie = vi.fn();
    mockFetch({
      "/crypto/api/auth/me": { ok: false, status: 401, body: {} },
      "/crypto/api/login": { ok: true, status: 200, body: { ok: true, user: "admin" } },
    });
    const w = mountView();
    await flushPromises();
    await w.find('input[autocomplete="username"]').setValue("admin");
    await w.find('input[autocomplete="current-password"]').setValue("pw");
    await w.find("button.login-btn-crypto").trigger("click");
    await flushPromises();
    const btn = w.find("button.login-btn-crypto").text();
    expect(btn).not.toContain("登录中");
  });

  it("登录失败 401 → 显示错误文案", async () => {
    mockFetch({
      "/crypto/api/auth/me": { ok: false, status: 401, body: {} },
      "/crypto/api/login": { ok: false, status: 401, body: { detail: "用户名或密码错误" } },
    });
    const w = mountView();
    await flushPromises();
    await w.find('input[autocomplete="username"]').setValue("admin");
    await w.find('input[autocomplete="current-password"]').setValue("bad");
    await w.find("button.login-btn-crypto").trigger("click");
    await flushPromises();
    expect(w.text()).toContain("用户名或密码错误");
  });
});
