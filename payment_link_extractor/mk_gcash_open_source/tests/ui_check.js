const { chromium } = require("playwright");
const os = require("os");
const path = require("path");

const base = process.env.MK_TEST_URL || "http://127.0.0.1:8932/";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function base64url(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function testToken(email) {
  return `${base64url({alg: "none", typ: "JWT"})}.${base64url({
    exp: Math.floor(Date.now() / 1000) + 3600,
    email,
    name: "Primary account",
  })}.syntheticSignature`;
}

function fakeJob() {
  const expires = Math.floor(Date.now() / 1000) + 300;
  return {
    job_id: "local_0123456789abcdef",
    done: false,
    warnings: [],
    queue: {running: 1, queued: 0},
    accounts: [
      {
        id: "acct_0123456789abcdef",
        email: "a-very-long-account-name-for-layout-validation@example.com",
        name: "Primary account",
        status: "success",
        current_step: "follow_redirect",
        steps: [],
        attempts_used: 1,
        error: "",
        link_ready: true,
        link: "https://m.gcash.com/gcash-login-web/index.html?netAuthId=layout-validation-value-with-a-long-query-string",
        expires_at: expires,
        qr_ready: true,
        qr_source: "gcash_page",
        qr_version: 1,
        qr_url: "/api/jobs/local_0123456789abcdef/accounts/acct_0123456789abcdef/qr.png",
        refresh_available: true,
        payment_status: "waiting_scan",
        payment_success: false,
      },
      {
        id: "acct_fedcba9876543210",
        email: "failed@example.com",
        name: "Failed account",
        status: "failed",
        current_step: "create_checkout",
        steps: [],
        attempts_used: 5,
        error: "代理出口质量不足或上游风控拒绝了本次请求，这是用于验证超长错误信息在窄屏下能够正常换行的模拟内容。",
        link_ready: false,
        link: "",
        qr_ready: false,
        refresh_available: false,
        payment_status: "unavailable",
        payment_success: false,
      },
      {
        id: "acct_1111111111111111",
        email: "running@example.com",
        name: "Running account",
        status: "running",
        current_step: "configure_taxes",
        steps: [{label: "同步 PH/PHP 税费", state: "active"}],
        attempts_used: 1,
        error: "",
        link_ready: false,
        link: "",
        qr_ready: false,
        refresh_available: false,
        payment_status: "unavailable",
        payment_success: false,
      },
    ],
  };
}

const qrPng = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl9sAAAAASUVORK5CYII=",
  "base64",
);

async function installApiFixture(page) {
  let job = fakeJob();
  await page.route(/\/api\/jobs(?:\/.*)?$/, route => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (pathname.endsWith("/qr.png")) {
      return route.fulfill({status: 200, contentType: "image/png", body: qrPng});
    }
    if (pathname.endsWith("/abandon") && pathname.includes("/accounts/")) {
      const id = pathname.split("/accounts/")[1].split("/")[0];
      const account = job.accounts.find(item => item.id === id);
      Object.assign(account, {
        payment_status: "abandoned",
        refresh_available: false,
        qr_ready: false,
        qr_url: "",
      });
      return route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify(account)});
    }
    if (pathname.endsWith("/abandon")) {
      job.accounts.forEach(account => {
        if (account.status === "success" && !account.payment_success) {
          Object.assign(account, {payment_status: "abandoned", refresh_available: false, qr_ready: false, qr_url: ""});
        }
      });
      return route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify({ok: true, abandoned: 1, job})});
    }
    if (pathname.endsWith("/refresh")) {
      return route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify(job.accounts[0])});
    }
    if (pathname.endsWith("/cancel")) {
      return route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify({ok: true, job})});
    }
    return route.fulfill({
      status: request.method() === "POST" ? 202 : 200,
      contentType: "application/json",
      body: JSON.stringify(job),
    });
  });
}

async function fillAndParse(page, proxyCount = 2) {
  await page.locator("#tokenInput").fill(`holder@example.com|${testToken("holder@example.com")}`);
  await page.locator("#proxyInput").fill(
    Array.from({length: proxyCount}, (_, index) => `proxy${index + 1}.example:8080`).join("\n"),
  );
  await page.locator("#importTokenBtn").click();
  await page.waitForSelector("#stage2.active .accountCard");
}

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 980}});
    page.setDefaultTimeout(8000);
    await installApiFixture(page);
    await page.goto(base, {waitUntil: "networkidle"});
    await page.waitForSelector("#queueStatus.online");
    assert(await page.title() === "MK提链 · 开源版", "unexpected title");
    const text = await page.locator("body").innerText();
    for (const banned of ["CDK", "平台节点", "管理员", "购买公告", "升级版"]) {
      assert(!text.includes(banned), `closed-source UI text remains: ${banned}`);
    }
    const logo = await page.locator(".brand img").evaluate(image => ({width: image.naturalWidth, height: image.naturalHeight}));
    assert(logo.width > 0 && logo.height > 0, "brand asset did not render");

    const browserToken = testToken("holder@example.com");
    await page.locator("#tokenInput").fill(`holder@example.com|${browserToken}`);
    await page.locator("#proxyInput").fill("proxy1.example:8080\nproxy2.example:8080");
    assert((await page.locator("#inputCounter").innerText()).includes("2 个节点"), "proxy counter did not update");
    const importShot = path.join(os.tmpdir(), "mk-gcash-open-import-desktop.png");
    await page.screenshot({path: importShot, fullPage: true});

    await page.locator("#importTokenBtn").click();
    await page.waitForSelector("#stage2.active .accountCard.selected");
    assert(!(await page.locator("body").innerText()).includes(browserToken), "AT leaked into rendered DOM");
    assert((await page.locator("#selectionBadge").innerText()).includes("1 / 1"), "account was not selected");
    assert(!(await page.locator("#startJob").isDisabled()), "start button is unexpectedly disabled");
    await page.locator("#startJob").click();
    await page.waitForSelector(".resultCard.success");
    assert(await page.locator(".resultCard").count() === 2, "finished result cards did not render");
    assert(await page.locator(".progressCard").count() === 1, "running progress card did not render");
    assert(await page.locator("#startJob").isDisabled(), "start button permits duplicate running job");

    await page.locator('[data-open-qr="acct_0123456789abcdef"]').click();
    await page.waitForSelector("#qrModal:not(.hidden)");
    const modal = await page.locator(".modalPanel").boundingBox();
    assert(modal && modal.width <= 440, "QR modal width is unstable");
    await page.locator("#closeQr").click();

    page.once("dialog", dialog => dialog.accept());
    await page.locator('[data-abandon="acct_0123456789abcdef"]').click();
    await page.locator('[data-abandon="acct_0123456789abcdef"]').waitFor({state: "detached"});
    const abandonedState = await page.locator(".resultCard.success .callbackState").innerText();
    assert(abandonedState.includes("已放弃支付监控"), `unexpected abandon state: ${abandonedState}`);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(2800);
    const resultsShot = path.join(os.tmpdir(), "mk-gcash-open-results-desktop.png");
    await page.screenshot({path: resultsShot, fullPage: true});

    const desktop = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      stageWidth: document.querySelector(".workspace")?.getBoundingClientRect().width || 0,
      overflowingButtons: [...document.querySelectorAll("button")]
        .filter(button => button.scrollWidth > button.clientWidth + 1)
        .map(button => button.textContent.trim()),
    }));
    assert(desktop.scrollWidth <= desktop.clientWidth, "desktop horizontal overflow");
    assert(desktop.stageWidth > 1000, "desktop workspace collapsed");
    assert(desktop.overflowingButtons.length === 0, `desktop button overflow: ${desktop.overflowingButtons.join(", ")}`);

    await page.setViewportSize({width: 390, height: 844});
    await page.reload({waitUntil: "networkidle"});
    await page.waitForSelector("#queueStatus.online");
    await fillAndParse(page, 1);
    await page.locator("#startJob").click();
    await page.waitForSelector(".resultCard.success");
    const mobile = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
      headerRight: document.querySelector(".titlebar")?.getBoundingClientRect().right || 0,
      viewport: window.innerWidth,
      resultWidth: document.querySelector(".resultCard")?.getBoundingClientRect().width || 0,
      overflowingButtons: [...document.querySelectorAll("button")]
        .filter(button => button.scrollWidth > button.clientWidth + 1)
        .map(button => button.textContent.trim()),
    }));
    assert(mobile.scrollWidth <= mobile.clientWidth, "mobile horizontal overflow");
    assert(mobile.headerRight <= mobile.viewport, "mobile header overflow");
    assert(mobile.resultWidth <= mobile.viewport - 20, "mobile result card overflow");
    assert(mobile.overflowingButtons.length === 0, `mobile button overflow: ${mobile.overflowingButtons.join(", ")}`);
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.waitForTimeout(2800);
    const mobileShot = path.join(os.tmpdir(), "mk-gcash-open-mobile.png");
    await page.screenshot({path: mobileShot, fullPage: true});

    process.stdout.write(JSON.stringify({importShot, resultsShot, mobileShot, desktop, mobile}));
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error.stack || error);
  process.exit(1);
});
