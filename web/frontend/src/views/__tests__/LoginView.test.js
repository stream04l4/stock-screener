// 登录门禁回归（v7.0.14）：App 根 auth 流程 + LoginView 交互。
//
// mock 点：api client（零网络）。流程契约：
// - 启动 → GET /api/auth/me：401 → 渲染登录页；200 → 直进 AppShell；
// - 登录提交 → POST /api/login：成功 → 系统选择页（共享登录双系统入口）→
//   选「A股4维选股」→ AppShell 挂载 + taskStore.restore（/api/health）；
//   失败 → 行内错误文案，停留登录页；
// - 运行中任意受保护 API 401 → client 发 auth:401 → 回登录页；
// - 顶栏「退出」→ POST /api/logout → 回登录页。
import { beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { createPinia } from "pinia";

const apiMock = vi.fn();
vi.mock("../../api/client.js", () => ({
  api: (...a) => apiMock(...a),
  isBackfillErr: (e) => !!(e && e.status === 409 && e.body && e.error === "lake_backfill_in_progress"),
}));

const App = (await import("../../App.vue")).default;
const { useAuthStore } = await import("../../stores/authStore.js");

function mockApi({ me = 401 } = {}) {
  apiMock.mockImplementation(async (path, opts = {}) => {
    if (path === "/api/auth/me") {
      if (me === 401) {
        const e = new Error("未登录或会话已过期");
        e.status = 401;
        throw e;
      }
      return { user: "admin" };
    }
    if (path === "/api/login" && opts.method === "POST") {
      const b = JSON.parse(opts.body);
      if (b.username === "admin" && b.password === "unit-test-only-pass") return { ok: true, user: "admin" };
      const e = new Error("用户名或密码错误");
      e.status = 401;
      throw e;
    }
    if (path === "/api/logout" && opts.method === "POST") return { ok: true };
    if (path === "/api/health") return { ok: true, active_task: null };
    throw new Error("unmocked: " + path);
  });
}

const mk = () => mount(App, { attachTo: document.body, global: { plugins: [createPinia()] } });
const isLogin = (w) => !!w.find(".login-card").exists();
const isChoice = (w) => !!w.find(".choice-wrap").exists();
const isShell = (w) => w.find(".topbar .brand").exists();

beforeEach(() => {
  apiMock.mockReset();
  mockApi();
});

describe("App 登录门禁", () => {
  it("未登录（me 401）→ 登录页；加载态不渲染页签", async () => {
    const w = mk();
    expect(isLogin(w)).toBe(false); // checked=false 加载中
    await flushPromises();
    expect(isLogin(w)).toBe(true);
    expect(w.find(".login-card").text()).toContain("深回撤金字塔加仓策略");
    w.unmount();
  });

  it("me 200（cookie 有效刷新）→ 直进主页", async () => {
    mockApi({ me: 200 });
    const w = mk();
    await flushPromises();
    expect(isShell(w)).toBe(true);
    w.unmount();
  });

  it("登录成功 → 主页；登录失败 → 停留登录页 + 错误文案", async () => {
    const w = mk();
    await flushPromises();
    expect(isLogin(w)).toBe(true);
    // 失败
    await w.find('input[autocomplete="username"]').setValue("admin");
    await w.find('input[type="password"]').setValue("wrong");
    await w.find("form.login-card").trigger("submit");
    await flushPromises();
    expect(isLogin(w)).toBe(true);
    expect(w.find(".login-err").text()).toContain("用户名或密码错误");
    // 成功
    await w.find('input[type="password"]').setValue("unit-test-only-pass");
    await w.find("form.login-card").trigger("submit");
    await flushPromises();
    // 登录成功 → 系统选择页（共享登录：A股4维选股 / Quant iTransformer）
    expect(isChoice(w)).toBe(true);
    expect(w.find(".choice-quant").text()).toContain("Quant iTransformer");
    // 选「A股4维选股」→ 进主页
    await w.find(".choice-stock").trigger("click");
    await flushPromises();
    expect(isShell(w)).toBe(true);
    expect(w.find(".topbar-user").text()).toContain("admin");
    // taskStore.restore 已走 /api/health
    expect(apiMock.mock.calls.some(([p]) => p === "/api/health")).toBe(true);
    w.unmount();
  });

  it("运行中 auth:401 事件 → 回登录页", async () => {
    mockApi({ me: 200 });
    const w = mk();
    await flushPromises();
    expect(isShell(w)).toBe(true);
    window.dispatchEvent(new CustomEvent("auth:401"));
    await flushPromises();
    expect(isLogin(w)).toBe(true);
    w.unmount();
  });

  it("顶栏退出 → POST /api/logout → 回登录页", async () => {
    mockApi({ me: 200 });
    const w = mk();
    await flushPromises();
    await w.find(".topbar-logout").trigger("click");
    await flushPromises();
    expect(apiMock.mock.calls.some(([p, o]) => p === "/api/logout" && (o || {}).method === "POST")).toBe(true);
    expect(isLogin(w)).toBe(true);
    w.unmount();
  });
});

describe("authStore 单元", () => {
  it("check/login/logout/onUnauthorized 状态迁移", async () => {
    mockApi({ me: 401 });
    const pinia = createPinia();
    const auth = useAuthStore(pinia);
    await auth.check();
    expect(auth.user).toBeNull();
    expect(auth.checked).toBe(true);
    const ok = await auth.login("admin", "unit-test-only-pass");
    expect(ok).toBe(true);
    expect(auth.user).toBe("admin");
    const bad = await auth.login("admin", "x");
    expect(bad).toBe(false);
    expect(auth.loginErr).toContain("用户名或密码错误");
    auth.onUnauthorized();
    expect(auth.user).toBeNull();
    // logout 走 POST（me 200 场景下 user 在位）
    await auth.login("admin", "unit-test-only-pass");
    await auth.logout();
    expect(auth.user).toBeNull();
  });
});
