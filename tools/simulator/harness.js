// @ts-check
/**
 * glasscast 本地模拟器 harness。
 *
 * 驱动 waft-lite 内核的流程（research.md §3）：
 *   import('/assets/index.blob-worker.js') → createWaftLite({canvas, locateFile, initTimeoutMs, print, printErr, on…})
 *   → [setCorsProxy(prefix)] → 等开机动画 → ccall('wasm_test_open_dsl', xml, dataJson, uri)
 *   → 从内核 stdout 里认 "DSL rendered successfully" / "Failed to render DSL" 判定结果
 *   → 同一 uri 再渲染前先 ccall('wasm_test_close_page') 弹栈。
 * 内核 API 的完整记录见同目录 README.md。
 *
 * 页面要 cross-origin isolated（serve.py 加 COOP/COEP），否则内核入口直接抛错。
 */

/** @typedef {'info'|'warn'|'error'} Level */
/** @typedef {'rendered'|'failed'|'timeout'|'superseded'} RenderOutcome */
/**
 * @typedef {{type:'status', status:string, detail?:string}
 *   | {type:'log', level:Level, source:'kernel'|'harness', message:string}
 *   | {type:'progress', message:string}
 *   | {type:'error', message:string}
 *   | {type:'render', uri:string, outcome:RenderOutcome, durationMs:number}
 *   | {type:'inspect', report:any}
 *   | {type:'waftStatus', category:string, data:any}
 *   | {type:'uiAction', action:any}
 *   | {type:'cameraRequest', action:string, detail:any}} HarnessEvent
 */
/**
 * 内核模块里本 harness 用到的部分（Emscripten Module + 入口包装加的方法）。
 * @typedef {{
 *   ccall: (ident:string, returnType:string|null, argTypes:string[], args:any[]) => any,
 *   setCorsProxy?: (prefix:string) => void,
 *   destroy?: () => void,
 *   getWaftStatus?: (category?:string) => any,
 *   _buildInfo?: any,
 *   _bootAnimCompleted?: boolean,
 *   FS?: any,
 * }} WaftModule
 */

/** 内核要求 uri 以此开头（wasm 字符串 "wasm_test_open_dsl: uri must start with %s"）。 */
export const WAFT_URI_PREFIX = 'waft://lite/dsl';

/** 内核按键码；state 默认 2。S1 手势：单击=confirm、双击=back、前滑=prev、后滑=next。 */
export const KEY_CODES = Object.freeze({ confirm: 10, back: 27, prev: 17, next: 18, home: 2, voice: 200, left: 20, right: 19 });

/**
 * 屏幕取景：内核把 canvas 设成 640×480，这里只露出 y∈[142, 432) 这一条，
 * 即 S1 一屏 640×290（research.md §2.3）。
 */
export const SCREEN = Object.freeze({ top: 142, width: 640, height: 290 });

/** 超时常量。 */
export const TIMEOUTS = Object.freeze({
  initMs: 60000,
  bootAnimMs: 30000,
  pageSettleMs: 5000,
  popSettleMs: 600,
  postPopInjectDelayMs: 120,
  closePageMaxPops: 10,
});

/** 勾选 addLatLng 时追加到 dataSource 的默认经纬度。 */
export const DEFAULT_LATLNG = Object.freeze({ latitude: 30.29388, longitude: 10.58672 });

const RE_RENDERED = /rendered successfully|render ok md5=/i;
const RE_RENDER_FAILED = /failed to render dsl|dsl render failed|render fail\w* md5=/i;
const RE_NO_PAGE = /no page to pop/;
const RE_CLOSE_STARTED = /pm_close_page:|\] on-hide/;
const RE_UNLOADED = /page_on_unload|\] on-unload/;

/** 关键日志分类：用于高亮和统计（模式来自 wasm 里的格式串，见 README）。 */
export const LINE_TAGS = Object.freeze([
  { tag: 'budget', re: /over the \d+ device limit|image too large|display-budget/i },
  { tag: 'download-fail', re: /download failed|download timeout|remote image failed|failed to start (batched |gif )?download|build_fetch_url failed|download busy|cloud asset download failed/i },
  { tag: 'download', re: /download success|batched download succ|gif download succ|start download, remote=/i },
  { tag: 'decode', re: /unknown image type|not support image format|decode failed|invalid png header|failed to load image/i },
  { tag: 'render', re: /rendered successfully|failed to render dsl|dsl render failed|render failed for component|failed to parse dsl/i },
  { tag: 'fset', re: /frame-set|fset (created|destroyed)|on_completed/i },
  { tag: 'image', re: /set_src|remote refresh|image (render|update)|enter image|creating image/i },
  { tag: 'data', re: /data:\/\//i },
  { tag: 'proxy', re: /cors_proxy/i },
  { tag: 'lvgl', re: /^\[(Warn|Error)\]/ },
]);

/**
 * 合法字符原样保留，否则转成 slug + djb2 哈希。
 * @param {string} s
 */
export function sanitizeUriSegment(s) {
  if (/^[A-Za-z0-9._-]+$/.test(s)) return s;
  const slug = s.replace(/[^A-Za-z0-9]+/g, '-').replace(/^-+|-+$/g, '').toLowerCase();
  let h = 5381;
  for (let i = 0; i < s.length; i += 1) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  const hash = h.toString(36);
  return slug ? `${slug}-${hash}` : `page-${hash}`;
}

/**
 * 拼出页面 uri：waft://lite/dsl/<app>/<page>。
 * @param {string} app
 * @param {string} page
 */
export function buildDslPageUri(app, page) {
  return `${WAFT_URI_PREFIX}/${sanitizeUriSegment(app)}/${sanitizeUriSegment(page)}`;
}

/** @param {string} line */
export function classifyLine(line) {
  /** @type {string[]} */
  const tags = [];
  for (const { tag, re } of LINE_TAGS) if (re.test(line)) tags.push(tag);
  return tags;
}

/**
 * 结构检查（浏览器 DOMParser）。实测内核对坏 XML 很宽容：标签没闭合也照样打 "render ok"，
 * 所以渲染前先查一遍，只报真正的结构错误。为不误报 DSL 合法写法，先做三处放宽：
 * 无值的 a:else 等布尔标志补成 =""；裸 & 转义（DSL 里 URL 推荐写裸 &，research.md §2.2）；
 * 根节点没声明 xmlns:a 时补上。
 * @param {string} xml
 * @returns {string|null} 错误摘要；没问题返回 null
 */
export function lintXml(xml) {
  if (typeof DOMParser === 'undefined') return null;
  let src = xml.replace(/(\s)(a:(?:else|if|elif|for|semantic))(?=[\s/>])/g, '$1$2=""');
  src = src.replace(/&(?!amp;|lt;|gt;|quot;|apos;|#\d+;|#x[0-9a-fA-F]+;)/g, '&amp;');
  if (!/xmlns:a\s*=/.test(src)) src = src.replace(/<([A-Za-z][\w-]*)/, '<$1 xmlns:a="waft://dsl"');
  const doc = new DOMParser().parseFromString(src, 'application/xml');
  const err = doc.getElementsByTagName('parsererror')[0];
  if (!err) return null;
  const text = (err.textContent || '').replace(/\s+/g, ' ').trim();
  // Chrome/WebKit 的格式："This page contains the following errors:error on line L at column C: … Below is …"
  const m = /(error on line \d+ at column \d+:.*?)(?: Below is a rendering|$)/i.exec(text);
  return `${(m ? m[1] : text || 'XML 解析错误').slice(0, 240)}（列号可能因放宽处理略有偏移）`;
}

/**
 * 内核驱动：启动内核、注入页面，并从内核日志判定渲染结果。
 */
export class WaftHarness {
  /**
   * @param {{
   *   canvas: HTMLCanvasElement,
   *   entryUrl: string,
   *   distBase: string,
   *   dataUrl: string,
   *   initTimeoutMs?: number,
   * }} opts
   */
  constructor(opts) {
    this.canvas = opts.canvas;
    this.entryUrl = opts.entryUrl;
    this.distBase = opts.distBase.endsWith('/') ? opts.distBase : `${opts.distBase}/`;
    this.dataUrl = opts.dataUrl;
    this.initTimeoutMs = opts.initTimeoutMs ?? TIMEOUTS.initMs;
    /** @type {Set<(ev: HarnessEvent) => void>} */
    this.listeners = new Set();
    /** @type {WaftModule|null} */
    this.module = null;
    /** @type {Promise<WaftModule>|null} */
    this.bootPromise = null;
    this.destroyed = false;
    this.bootAnimResolved = false;
    /** @type {Array<() => void>} */
    this.bootAnimWaiters = [];
    /** @type {string|null} */ this.currentXml = null;
    /** @type {string|null} */ this.currentUri = null;
    /** @type {any} */ this.currentData = null;
    /** @type {((o: RenderOutcome) => void)|null} */ this.pageSettleResolve = null;
    /** @type {number|null} */ this.pageSettleTimer = null;
    /** @type {(() => void)|null} */ this.popAckResolve = null;
    this.popCloseStarted = false;
    this.popUnloaded = false;
    this.popNoPage = false;
    this.inspectActive = false;
    /** @type {Promise<any>} */
    this.loadChain = Promise.resolve();
    this.bootMs = 0;
  }

  /** @param {(ev: HarnessEvent) => void} fn */
  on(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** @param {HarnessEvent} ev */
  emit(ev) {
    for (const fn of this.listeners) {
      try { fn(ev); } catch (e) { console.error('[harness] listener threw', e); }
    }
  }

  /** @param {Level} level @param {string} message */
  log(level, message) {
    this.emit({ type: 'log', level, source: 'harness', message });
  }

  /** @returns {Promise<WaftModule>} */
  boot() {
    if (this.module) return Promise.resolve(this.module);
    if (this.bootPromise) return this.bootPromise;
    this.emit({ type: 'status', status: 'booting' });
    const t0 = performance.now();
    this.bootPromise = (async () => {
      if (globalThis.crossOriginIsolated !== true) {
        throw new Error('页面没有 cross-origin isolated（缺 COOP/COEP 头）。请用 tools/simulator/serve.py 打开本页。');
      }
      /** @type {any} */
      let ns;
      try {
        ns = await import(this.entryUrl);
      } catch (e) {
        throw new Error(`内核入口加载失败（${this.entryUrl}）：${errText(e)}。先运行 tools/simulator/fetch_assets.sh`);
      }
      const factory = ns.default ?? ns.createWaftLite;
      if (typeof factory !== 'function') throw new Error('内核入口没有导出 createWaftLite（default export）');
      /** @type {WaftModule} */
      const mod = await factory({
        canvas: this.canvas,
        // Emscripten 用空前缀请求 .data，所以 .data 必须显式给出；.wasm 和其它走 dist/。
        locateFile: (/** @type {string} */ path) => (path === 'waft-lite-wasm.data' ? this.dataUrl : this.distBase + path),
        initTimeoutMs: this.initTimeoutMs,
        // 自带 print 会绕过入口里的 essentialOnlyPrint 过滤，拿到 C 侧放行的全部 stdout。
        print: (/** @type {string} */ line) => this.handleKernelLine(line, 'info'),
        printErr: (/** @type {string} */ line) => this.handleKernelLine(line, 'warn'),
        // Emscripten 资源包下载进度（"Downloading data... (a/b)"）。
        setStatus: (/** @type {string} */ text) => { if (text) this.emit({ type: 'progress', message: String(text) }); },
        onAbort: (/** @type {any} */ what) => this.emit({ type: 'error', message: `内核异常终止${what ? `：${String(what)}` : ''}` }),
        onBootAnimComplete: () => {
          this.log('info', '开机动画完成，桌面就绪');
          this.resolveBootAnim();
        },
        onWaftStatus: (/** @type {string} */ category, /** @type {any} */ data) => {
          if (category === 'elementInspect') {
            this.inspectActive = !!(data && data.hit);
            this.emit({ type: 'inspect', report: data });
          } else {
            this.emit({ type: 'waftStatus', category, data });
          }
        },
        onCameraRequest: (/** @type {string} */ action, /** @type {any} */ detail) => this.emit({ type: 'cameraRequest', action, detail }),
        onUiAction: (/** @type {any} */ action) => this.emit({ type: 'uiAction', action }),
      });
      if (!mod || typeof mod.ccall !== 'function') throw new Error('内核模块缺少 ccall');
      if (this.destroyed) {
        try { mod.destroy?.(); } catch { /* ignore */ }
        throw new Error('harness 已销毁');
      }
      this.module = mod;
      if (mod._bootAnimCompleted) this.resolveBootAnim();
      this.bootMs = Math.round(performance.now() - t0);
      this.emit({ type: 'status', status: 'ready', detail: `${this.bootMs} ms` });
      return mod;
    })();
    this.bootPromise.catch((e) => {
      this.bootPromise = null;
      this.emit({ type: 'error', message: errText(e) });
      this.emit({ type: 'status', status: 'error', detail: errText(e) });
    });
    return this.bootPromise;
  }

  resolveBootAnim() {
    if (this.bootAnimResolved) return;
    this.bootAnimResolved = true;
    const waiters = this.bootAnimWaiters;
    this.bootAnimWaiters = [];
    waiters.forEach((w) => w());
  }

  /** 等开机动画；超时也放行（30 s 兜底）。 */
  waitBootAnim() {
    if (this.bootAnimResolved || this.module?._bootAnimCompleted) {
      this.bootAnimResolved = true;
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      let done = false;
      const finish = () => { if (!done) { done = true; resolve(undefined); } };
      this.bootAnimWaiters.push(finish);
      setTimeout(() => {
        if (!done) this.log('warn', `等开机动画超时（${TIMEOUTS.bootAnimMs} ms），直接注入`);
        finish();
      }, TIMEOUTS.bootAnimMs);
    });
  }

  /**
   * 处理内核的每行 stdout/stderr：按行首标记提升级别，推进弹栈等待，判定渲染结果。
   * @param {any} raw @param {Level} level
   */
  handleKernelLine(raw, level) {
    const line = String(raw);
    // C 侧日志行形如 "[I] [ess:tag] …"；[W]/[E] 提升级别（print 回调本身不区分级别）。
    const marked = /^\[E\]/.test(line) ? 'error' : /^\[W\]/.test(line) ? 'warn' : level;
    this.emit({ type: 'log', level: marked, source: 'kernel', message: line });
    if (this.popAckResolve) {
      if (RE_NO_PAGE.test(line)) {
        this.popNoPage = true;
        this.popAckResolve();
      } else {
        if (RE_CLOSE_STARTED.test(line)) this.popCloseStarted = true;
        if (RE_UNLOADED.test(line)) this.popUnloaded = true;
        if (this.popCloseStarted && this.popUnloaded) this.popAckResolve();
      }
    }
    if (RE_RENDERED.test(line)) this.settlePage('rendered');
    else if (RE_RENDER_FAILED.test(line)) this.settlePage('failed');
  }

  /** @param {RenderOutcome} [outcome] */
  settlePage(outcome = 'superseded') {
    if (this.pageSettleTimer !== null) {
      clearTimeout(this.pageSettleTimer);
      this.pageSettleTimer = null;
    }
    const resolve = this.pageSettleResolve;
    this.pageSettleResolve = null;
    resolve?.(outcome);
  }

  /** @returns {Promise<RenderOutcome>} */
  waitRenderOutcome() {
    this.settlePage();
    return new Promise((resolve) => {
      this.pageSettleResolve = resolve;
      this.pageSettleTimer = window.setTimeout(() => this.settlePage('timeout'), TIMEOUTS.pageSettleMs);
    });
  }

  /** 弹出当前页（ccall wasm_test_close_page）；等 close+unload 两条日志或 600 ms。 */
  popCurrentPage() {
    const mod = this.module;
    if (!mod) return Promise.resolve();
    return new Promise((resolve) => {
      let finished = false;
      /** @type {number|null} */
      let timer = null;
      const done = () => {
        if (finished) return;
        finished = true;
        this.popAckResolve = null;
        if (timer !== null) clearTimeout(timer);
        if (this.popCloseStarted) setTimeout(resolve, TIMEOUTS.postPopInjectDelayMs);
        else resolve(undefined);
      };
      this.popCloseStarted = false;
      this.popUnloaded = false;
      this.popNoPage = false;
      this.popAckResolve = done;
      timer = window.setTimeout(done, TIMEOUTS.popSettleMs);
      let rc;
      try {
        rc = mod.ccall('wasm_test_close_page', 'number', [], []);
      } catch (e) {
        this.log('warn', `close_page 调用异常，跳过 pop：${errText(e)}`);
        done();
        return;
      }
      if (rc === 0) {
        this.log('warn', '内核拒绝 close_page 入队（队列已满），跳过 pop');
        done();
      }
    });
  }

  /**
   * 原始注入：ccall('wasm_test_open_dsl', null, ['string','string','string'], [xml, dataJson, uri])。
   * @param {string} xml @param {string} dataJson @param {string} uri
   */
  inject(xml, dataJson, uri) {
    if (!this.module) throw new Error('内核未就绪');
    if (!uri.startsWith(`${WAFT_URI_PREFIX}/`)) {
      throw new Error(`DSL 页面 URI 不合法（${uri}）：内核要求以 ${WAFT_URI_PREFIX}/ 开头，形如 ${WAFT_URI_PREFIX}/<app>/<page>`);
    }
    this.module.ccall('wasm_test_open_dsl', null, ['string', 'string', 'string'], [xml, dataJson, uri]);
  }

  /**
   * 串行化的渲染：boot → 等开机动画 →（同 uri 先 pop）→ inject → 等结果；超时则 pop 后补注入一次。
   * @param {{xml:string, data:any, uri:string}} req
   * @returns {Promise<RenderOutcome>}
   */
  loadTemplate(req) {
    const run = () => this.loadTemplateInner(req);
    const p = this.loadChain.then(run, run);
    this.loadChain = p.catch(() => undefined);
    return p;
  }

  /** @param {{xml:string, data:any, uri:string}} req @returns {Promise<RenderOutcome>} */
  async loadTemplateInner(req) {
    const t0 = performance.now();
    await this.boot();
    await this.waitBootAnim();
    this.emit({ type: 'status', status: 'rendering' });
    if (this.currentUri !== null && this.currentUri === req.uri) await this.popCurrentPage();
    this.currentXml = req.xml;
    this.currentUri = req.uri;
    this.currentData = req.data ?? {};
    const dataJson = JSON.stringify(req.data ?? {});
    this.inject(req.xml, dataJson, req.uri);
    let outcome = await this.waitRenderOutcome();
    if (outcome === 'timeout') {
      this.log('warn', '页面渲染未确认（等待超时），补救：pop 后重注入一次');
      await this.popCurrentPage();
      this.inject(req.xml, dataJson, req.uri);
      outcome = await this.waitRenderOutcome();
      if (outcome !== 'rendered') this.log('warn', `页面渲染仍未确认（${outcome}）`);
    }
    const durationMs = Math.round(performance.now() - t0);
    this.emit({ type: 'render', uri: req.uri, outcome, durationMs });
    this.emit({ type: 'status', status: outcome === 'rendered' ? 'rendered' : outcome === 'failed' ? 'failed' : 'unconfirmed', detail: `${durationMs} ms` });
    return outcome;
  }

  /** 只换数据：同 xml/uri 直接再注入（不 pop）。 @param {any} data */
  updateData(data) {
    const run = async () => {
      if (!this.module || this.currentXml === null || this.currentUri === null) throw new Error('还没有渲染过页面');
      this.currentData = data;
      this.inject(this.currentXml, JSON.stringify(data ?? {}), this.currentUri);
    };
    const p = this.loadChain.then(run, run);
    this.loadChain = p.catch(() => undefined);
    return p;
  }

  /** 连续 pop 直到内核说没页面了（最多 10 次）。 */
  closePage() {
    const run = async () => {
      await this.boot();
      for (let i = 0; i < TIMEOUTS.closePageMaxPops; i += 1) {
        await this.popCurrentPage();
        if (this.popNoPage || (!this.popCloseStarted && !this.popUnloaded)) break;
      }
      this.clearInspect();
      this.currentXml = null;
      this.currentUri = null;
      this.currentData = null;
      this.emit({ type: 'status', status: 'ready', detail: '页面已关闭' });
    };
    const p = this.loadChain.then(run, run);
    this.loadChain = p.catch(() => undefined);
    return p;
  }

  /** @param {keyof typeof KEY_CODES} name @param {number} [state] */
  sendKey(name, state = 2) {
    if (!this.module) return;
    const code = KEY_CODES[name];
    const rc = this.module.ccall('wasm_send_key', 'number', ['number', 'number'], [code, state]);
    if (rc === 0) this.log('warn', `内核拒绝按键注入（key=${name}, code=${code}）`);
    else this.log('info', `按键 ${name}（code=${code}, state=${state}）`);
  }

  /** 画布坐标（含 142 的顶部偏移）。结果经 onWaftStatus('elementInspect', …) 回来。 @param {number} x @param {number} y */
  inspectAt(x, y) {
    if (!this.module) return;
    const rc = this.module.ccall('wasm_inspect_click', 'number', ['number', 'number'], [Math.round(x), Math.round(y)]);
    if (rc === 0) this.log('warn', `内核拒绝元素检查入队（x=${x}, y=${y}）`);
  }

  clearInspect() {
    if (!this.module || !this.inspectActive) return;
    this.inspectActive = false;
    try { this.module.ccall('wasm_inspect_clear', 'number', [], []); } catch (e) { this.log('warn', errText(e)); }
    this.emit({ type: 'inspect', report: null });
  }

  /** 远程资源代理前缀（入口包装：ccall('waft_download_set_cors_proxy', null, ['string'], [prefix])）。 @param {string} prefix */
  setCorsProxy(prefix) {
    if (!this.module?.setCorsProxy) throw new Error('内核未就绪或不支持 setCorsProxy');
    this.module.setCorsProxy(prefix);
  }

  destroy() {
    if (this.destroyed) return;
    this.destroyed = true;
    const mod = this.module;
    this.module = null;
    this.bootPromise = null;
    this.settlePage();
    try { mod?.destroy?.(); } catch (e) { console.warn('[harness] destroy 异常', e); }
    this.emit({ type: 'status', status: 'destroyed' });
    this.listeners.clear();
  }
}

/** @param {unknown} e */
function errText(e) {
  return e instanceof Error ? e.message : String(e);
}

// ===========================================================================
// UI
// ===========================================================================

/** 内置页面副本：pages/ 下没有对应页面时用（cast_clip 是已下线的第一轮设计，只在这里保留，供对照实验）。 */
const BUILTIN_XML = Object.freeze({
  'cast-image': `<view xmlns:a="waft://dsl" width="100%" height="100%" flex-flow="column" bg-color="#000000">
  <page-meta screen-awake="keep"/>
  <tick var="t" duration="3600" refresh="second"/>
  <image src="{{url}}?t={{tick:t}}" width="100%" height="100%" image-fit="stretch"/>
</view>
`,
  cast_clip: `<view xmlns:a="waft://dsl" width="100%" height="100%" flex-flow="column" bg-color="#000000">
  <page-meta screen-awake="keep"/>
  <data i="0"/>
  <frame-set src="{{clip_url}}?i={{d:i}}"
             width="640" height="290"
             total-frames="10" frame-duration="200"
             repeat-count="1" color-format="0" load-async="true"
             on-completed="data://update?i={{d:i + 1}}"/>
</view>
`,
  // 离线自检：资源包内置 /external/img/chat_bar/genie_ball.fset（ext:// 映射到 /external/img/），
  // 加一个 tick 计数，确认内核能渲染、能动。
  selftest: `<view xmlns:a="waft://dsl" width="100%" height="100%" flex-flow="column" flex-main="center" flex-cross="center" bg-color="#000000">
  <page-meta screen-awake="keep"/>
  <tick var="t" duration="3600" refresh="second"/>
  <label text="{{title}}" width="600" height="40" font-size="28" color="#FFFFFF"/>
  <label text="tick {{tick:t}}" width="600" height="36" font-size="24" color="#7CFC9A"/>
  <frame-set src="ext://chat_bar/genie_ball.fset" width="32" height="30" total-frames="48" frame-duration="41" repeat-count="0" color-format="1"/>
</view>
`,
  // 故意超预算的远程图（serve.py /demo/big.png = 700×300 RGBA，解码 840 000 B > 742 400 B）。
  budget: `<view xmlns:a="waft://dsl" width="100%" height="100%" flex-flow="column" bg-color="#000000">
  <label text="下面这张图超预算，应被拒绝并在日志里告警" width="600" height="32" font-size="20" color="#FFFFFF"/>
  <image src="{{url}}" width="100%" height="200" image-fit="stretch"/>
</view>
`,
});

/**
 * @typedef {{
 *   id: string, label: string, page: string,
 *   xml: () => Promise<string>,
 *   data: () => Promise<any>,
 * }} Preset
 */

/** @typedef {{assets:{ok:boolean, missing:string[], invalid:Record<string,string>, build_info:any},
 *   proxy:{enabled:boolean, prefix:string, allow_private:boolean}, pillow:boolean,
 *   demo:{width:number,height:number,frames:number,image:string,image_png:string,clip:string,clip_jpeg:string,over_budget:string},
 *   pages:{name:string, template:string, data?:string}[], samples:string[], server_time:number}} ServerInfo */

const $ = (/** @type {string} */ id) => /** @type {HTMLElement} */ (document.getElementById(id));
const els = {
  status: $('status'), build: $('build'), banner: $('banner'),
  preset: /** @type {HTMLSelectElement} */ ($('preset')), loadPreset: $('loadPreset'),
  app: /** @type {HTMLInputElement} */ ($('app')), page: /** @type {HTMLInputElement} */ ($('page')), uri: $('uri'),
  xml: /** @type {HTMLTextAreaElement} */ ($('xml')), data: /** @type {HTMLTextAreaElement} */ ($('data')),
  render: /** @type {HTMLButtonElement} */ ($('render')), update: /** @type {HTMLButtonElement} */ ($('update')),
  close: /** @type {HTMLButtonElement} */ ($('close')), reboot: $('reboot'),
  useProxy: /** @type {HTMLInputElement} */ ($('useProxy')), addLatLng: /** @type {HTMLInputElement} */ ($('addLatLng')),
  proxyHint: $('proxyHint'),
  scale: /** @type {HTMLSelectElement} */ ($('scale')), fullCanvas: /** @type {HTMLInputElement} */ ($('fullCanvas')),
  inspect: /** @type {HTMLInputElement} */ ($('inspect')),
  screenCrop: $('screenCrop'), screenScale: $('screenScale'),
  canvas: /** @type {HTMLCanvasElement} */ ($('waft-canvas')), inspectBox: $('inspectBox'), inspectInfo: $('inspectInfo'),
  stats: $('stats'), requests: $('requests'),
  log: $('log'), logFilter: /** @type {HTMLInputElement} */ ($('logFilter')),
  logImportant: /** @type {HTMLInputElement} */ ($('logImportant')), logFollow: /** @type {HTMLInputElement} */ ($('logFollow')),
  clearLog: $('clearLog'), copyLog: $('copyLog'),
};

const params = new URLSearchParams(location.search);
const STORAGE_KEY = 'glasscast-sim:v1';
const MAX_LOG = 4000;
const T0 = performance.now();

/** @type {{t:number, level:Level, source:string, message:string, tags:string[]}[]} */
const logLines = [];
const counters = { kernelLines: 0, renders: 0, rendered: 0, failed: 0, unconfirmed: 0, lastRenderMs: 0, budget: 0, download: 0, downloadFail: 0, decode: 0 };
/** @type {Record<string, number[]>} 每个 demo 路径的服务器时间戳 */
const fetchTimes = {};
/** @type {ServerInfo|null} */
let info = null;
/** @type {Preset[]} */
let presets = [];
let lastRequestTs = 0;

const harness = new WaftHarness({
  canvas: els.canvas,
  entryUrl: `/assets/${params.get('entry') || 'index.blob-worker.js'}`,
  distBase: '/assets/dist/',
  dataUrl: '/assets/dist/waft-lite-wasm.data',
  initTimeoutMs: Number(params.get('initTimeoutMs')) || TIMEOUTS.initMs,
});

// ---- 日志 ----

/** @param {{t:number, level:Level, source:string, message:string, tags:string[]}} entry */
function logMatches(entry) {
  if (els.logImportant.checked && entry.tags.length === 0 && entry.level === 'info' && entry.source === 'kernel') return false;
  const f = els.logFilter.value.trim();
  if (!f) return true;
  try { return new RegExp(f, 'i').test(entry.message); } catch { return entry.message.toLowerCase().includes(f.toLowerCase()); }
}

/** @param {{t:number, level:Level, source:string, message:string, tags:string[]}} entry */
function logNode(entry) {
  const div = document.createElement('div');
  const cls = [];
  if (entry.level === 'warn' || entry.tags.includes('budget') || entry.tags.includes('download-fail') || entry.tags.includes('decode')) cls.push('warn');
  if (entry.level === 'error') cls.push('error');
  if (entry.source === 'harness') cls.push('harness');
  else if (entry.tags.length && !cls.length) cls.push('hl');
  div.className = cls.join(' ');
  const t = document.createElement('span');
  t.className = 't';
  t.textContent = (entry.t / 1000).toFixed(3).padStart(8, ' ');
  div.append(t, document.createTextNode(`${entry.source === 'harness' ? '[harness] ' : ''}${entry.message}`));
  return div;
}

/** @param {Level} level @param {string} source @param {string} message */
function appendLog(level, source, message) {
  const entry = { t: performance.now() - T0, level, source, message, tags: source === 'kernel' ? classifyLine(message) : [] };
  logLines.push(entry);
  if (source === 'kernel') {
    counters.kernelLines += 1;
    if (entry.tags.includes('budget')) counters.budget += 1;
    if (entry.tags.includes('download')) counters.download += 1;
    if (entry.tags.includes('download-fail')) counters.downloadFail += 1;
    if (entry.tags.includes('decode')) counters.decode += 1;
  }
  if (logLines.length > MAX_LOG) {
    logLines.splice(0, logLines.length - MAX_LOG);
    if (els.log.childElementCount > MAX_LOG) els.log.firstElementChild?.remove();
  }
  if (logMatches(entry)) {
    els.log.append(logNode(entry));
    if (els.logFollow.checked) els.log.scrollTop = els.log.scrollHeight;
  }
  scheduleStats();
}

function rerenderLog() {
  const frag = document.createDocumentFragment();
  for (const entry of logLines) if (logMatches(entry)) frag.append(logNode(entry));
  els.log.replaceChildren(frag);
  if (els.logFollow.checked) els.log.scrollTop = els.log.scrollHeight;
}

/** @param {Level} level @param {string} message */
const uiLog = (level, message) => appendLog(level, 'harness', message);

// ---- 状态 / 统计 ----

/** @param {string} text @param {''|'ok'|'warn'|'err'} [kind] */
function setStatus(text, kind = '') {
  els.status.textContent = text;
  els.status.className = `pill ${kind}`;
}

/** @param {string} msg */
function showBanner(msg) {
  els.banner.textContent = msg;
  els.banner.classList.toggle('show', !!msg);
}

let statsQueued = false;
function scheduleStats() {
  if (statsQueued) return;
  statsQueued = true;
  requestAnimationFrame(() => { statsQueued = false; renderStats(); });
}

/**
 * 最近 10 个间隔的中位数（首次渲染会连发 3 个请求，用平均数会被拉低）。
 * @param {number[]} ts
 */
function intervalSummary(ts) {
  if (ts.length < 2) return '—';
  const recent = ts.slice(-11);
  const d = [];
  for (let i = 1; i < recent.length; i += 1) d.push(recent[i] - recent[i - 1]);
  d.sort((a, b) => a - b);
  const mid = Math.floor(d.length / 2);
  const median = d.length % 2 ? d[mid] : (d[mid - 1] + d[mid]) / 2;
  return `${median.toFixed(2)} s`;
}

function renderStats() {
  const screenTs = Object.entries(fetchTimes).filter(([k]) => k.startsWith('/demo/screen')).flatMap(([, v]) => v).sort((a, b) => a - b);
  const clipTs = Object.entries(fetchTimes).filter(([k]) => k.startsWith('/demo/clip')).flatMap(([, v]) => v).sort((a, b) => a - b);
  /** @type {[string, string][]} */
  const items = [
    ['渲染（成功/失败/未确认）', `${counters.rendered}/${counters.failed}/${counters.unconfirmed}`],
    ['上次渲染耗时', counters.lastRenderMs ? `${counters.lastRenderMs} ms` : '—'],
    ['内核日志行', String(counters.kernelLines)],
    // essential-only 内核不打印下载成功的明细，只能统计失败告警；成功与否看服务端拉取记录。
    ['下载失败告警', String(counters.downloadFail)],
    ['预算告警', String(counters.budget)],
    ['解码告警', String(counters.decode)],
    ['demo 图被拉取', `${screenTs.length} 次`],
    ['demo 图拉取间隔（中位数）', intervalSummary(screenTs)],
    ['demo .fset 被拉取', `${clipTs.length} 次`],
    ['demo .fset 拉取间隔（中位数）', intervalSummary(clipTs)],
  ];
  els.stats.replaceChildren(...items.map(([k, v]) => {
    const d = document.createElement('div');
    d.className = 'stat';
    const kk = document.createElement('div'); kk.className = 'k'; kk.textContent = k;
    const vv = document.createElement('div'); vv.className = 'v'; vv.textContent = v;
    d.append(kk, vv);
    return d;
  }));
}

// ---- 拉取记录（serve.py /api/requests）----

/** @type {any[]} */
const requestRows = [];
async function pollRequests() {
  try {
    const res = await fetch(`/api/requests?since=${lastRequestTs}`, { cache: 'no-store' });
    if (res.ok) {
      const body = await res.json();
      for (const item of body.items) {
        lastRequestTs = Math.max(lastRequestTs, item.ts);
        const key = item.path;
        const list = (fetchTimes[key] ||= []);
        const prev = list.length ? list[list.length - 1] : null;
        list.push(item.ts);
        if (list.length > 200) list.splice(0, list.length - 200);
        requestRows.push({ ...item, delta: prev === null ? null : item.ts - prev });
      }
      if (body.items.length) {
        if (requestRows.length > 60) requestRows.splice(0, requestRows.length - 60);
        renderRequests();
        scheduleStats();
      }
    }
  } catch { /* 服务器停了就静默 */ }
  setTimeout(pollRequests, 1000);
}

function renderRequests() {
  const rows = requestRows.slice().reverse().map((r) => {
    const tr = document.createElement('tr');
    const when = new Date(r.ts * 1000).toLocaleTimeString('zh-CN', { hour12: false });
    const path = r.kind === 'proxy' ? `proxy → ${r.target}` : `${r.path}${r.query ? `?${r.query}` : ''}`;
    for (const [text, wrap] of /** @type {[string, boolean][]} */ ([
      [when, false],
      [r.delta === null ? '—' : `${r.delta.toFixed(2)} s`, false],
      [path, true],
      [String(r.status) + (r.error ? ` ${r.error}` : ''), true],
      [String(r.bytes ?? ''), false],
    ])) {
      const td = document.createElement('td');
      td.textContent = text;
      if (wrap) td.className = 'wrap';
      tr.append(td);
    }
    return tr;
  });
  els.requests.replaceChildren(...rows);
}

// ---- 屏幕取景 ----

function applyScreenLayout() {
  const s = Number(els.scale.value) || 1;
  const cw = els.canvas.width || 640;
  const ch = els.canvas.height || 480;
  const full = els.fullCanvas.checked;
  const top = full ? 0 : SCREEN.top;
  const h = full ? ch : SCREEN.height;
  els.screenCrop.style.width = `${cw * s}px`;
  els.screenCrop.style.height = `${Math.ceil(h * s)}px`;
  els.screenScale.style.width = `${cw}px`;
  els.screenScale.style.height = `${ch}px`;
  els.screenScale.style.transform = `translateY(${-top * s}px) scale(${s})`;
  els.screenCrop.classList.toggle('inspecting', els.inspect.checked);
}

/** @param {any} report */
function showInspect(report) {
  if (!report || report.hit !== true) {
    els.inspectBox.hidden = true;
    els.inspectInfo.textContent = report === null ? '' : '（没有命中元素）';
    return;
  }
  const n = (/** @type {any} */ v) => (Number.isFinite(Number(v)) ? Number(v) : 0);
  Object.assign(els.inspectBox.style, { left: `${n(report.x)}px`, top: `${n(report.y)}px`, width: `${n(report.w)}px`, height: `${n(report.h)}px` });
  els.inspectBox.hidden = false;
  const attrs = report.attrs && typeof report.attrs === 'object'
    ? Object.entries(report.attrs).map(([k, v]) => `${k}="${v}"`).join(' ')
    : '';
  els.inspectInfo.textContent = `<${report.tag}${attrs ? ` ${attrs}` : ''}>  行 ${report.line ?? '?'}  @ (${n(report.x)}, ${n(report.y)}) ${n(report.w)}×${n(report.h)}`;
}

// ---- 编辑器状态 ----

function currentUri() {
  return buildDslPageUri(els.app.value.trim() || 'glasscast', els.page.value.trim() || 'page');
}

function refreshUri() {
  els.uri.textContent = currentUri();
}

function saveState() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      xml: els.xml.value, data: els.data.value, app: els.app.value, page: els.page.value,
      preset: els.preset.value, scale: els.scale.value, fullCanvas: els.fullCanvas.checked,
      useProxy: els.useProxy.checked, addLatLng: els.addLatLng.checked,
    }));
  } catch { /* 隐私模式等：忽略 */ }
}

/** @returns {any} */
function loadState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

/** @returns {any} */
function parseDataOrThrow() {
  const text = els.data.value.trim() || '{}';
  let data;
  try {
    data = JSON.parse(text);
  } catch (e) {
    throw new Error(`data.json 不是合法 JSON：${errText(e)}`);
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('data.json 必须是 JSON 对象');
  return els.addLatLng.checked ? { ...data, ...DEFAULT_LATLNG } : data;
}

// ---- 预设 ----

/** @param {string} url */
async function fetchText(url) {
  const res = await fetch(url, { cache: 'no-store' });
  if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
  return res.text();
}

/** @param {string} name @returns {Promise<string>} */
async function pageXml(name) {
  const page = info?.pages.find((p) => p.name === name);
  if (page) {
    try { return await fetchText(page.template); } catch (e) { uiLog('warn', `读取 ${page.template} 失败，改用内置副本：${errText(e)}`); }
  }
  return /** @type {Record<string,string>} */ (BUILTIN_XML)[name];
}

/** @param {ServerInfo|null} srv @returns {Preset[]} */
function buildPresets(srv) {
  const origin = location.origin;
  const glasscast = params.get('glasscast') || 'http://127.0.0.1:8766/s/REPLACE_KEY';
  const hasPage = (/** @type {string} */ n) => !!srv?.pages.some((p) => p.name === n);
  const src = (/** @type {string} */ n) => (hasPage(n) ? `pages/${n}` : '内置副本');
  // 流畅模式页面 cast-emit（倒数换图）没有内置副本：只有 pages/cast-emit 存在时才给它的预设。
  // hops=12 与 start_cast(mode="fast", fps=4) 的出参相同：每换一张图倒数 12×3 = 36 步。
  const emit = hasPage('cast-emit');
  /** @type {(Preset|false)[]} */
  const list = [
    { id: 'selftest', label: '内核自检（离线：内置 .fset + tick 计数）', page: 'selftest',
      xml: async () => BUILTIN_XML.selftest, data: async () => ({ title: 'glasscast 模拟器自检' }) },
    // —— 现行的两个投屏页面 ——
    { id: 'cast_image_demo', label: `cast-image（${src('cast-image')}，普通模式）+ 本地演示图 /demo/screen.jpg`, page: 'cast-image',
      xml: () => pageXml('cast-image'), data: async () => ({ url: `${origin}/demo/screen.jpg` }) },
    emit && { id: 'cast_emit_demo', label: 'cast-emit（pages/cast-emit，流畅模式·倒数换图）+ 本地演示图，hops=12', page: 'cast-emit',
      xml: () => pageXml('cast-emit'), data: async () => ({ url: `${origin}/demo/screen.jpg`, hops: 12 }) },
    { id: 'cast_image_glasscast', label: 'cast-image + 本机 glasscast（改 data 里的 key）', page: 'cast-image',
      xml: () => pageXml('cast-image'), data: async () => ({ url: `${glasscast}/screen.jpg` }) },
    emit && { id: 'cast_emit_glasscast', label: 'cast-emit + 本机 glasscast（改 data 里的 key），hops=12', page: 'cast-emit',
      xml: () => pageXml('cast-emit'), data: async () => ({ url: `${glasscast}/screen.jpg`, hops: 12 }) },
    // —— 自检与实验 ——
    { id: 'budget', label: '预算超限自检：/demo/big.png 应被拒绝并告警', page: 'budget',
      xml: async () => BUILTIN_XML.budget, data: async () => ({ url: `${origin}/demo/big.png` }) },
    { id: 'cast_image_gray', label: `cast-image（${src('cast-image')}）+ 灰度 JPEG /demo/screen-gray.jpg（实验）`, page: 'cast-image',
      xml: () => pageXml('cast-image'), data: async () => ({ url: `${origin}/demo/screen-gray.jpg` }) },
    { id: 'cast_clip_demo', label: `cast_clip（${src('cast_clip')}，已下线）+ 本地演示帧合集 /demo/clip/<时间戳>.fset`, page: 'cast_clip',
      xml: () => pageXml('cast_clip'), data: async () => ({ clip_url: `${origin}/demo/clip/${Date.now()}.fset` }) },
    { id: 'cast_clip_jpeg', label: `cast_clip（${src('cast_clip')}）+ JPEG 帧 /demo/clip-jpeg.fset（实验）`, page: 'cast_clip',
      xml: () => pageXml('cast_clip'), data: async () => ({ clip_url: `${origin}/demo/clip-jpeg/${Date.now()}.fset` }) },
    { id: 'cast_clip_gray', label: `cast_clip（${src('cast_clip')}）+ 灰度 PNG 帧 /demo/clip-gray.fset（实验）`, page: 'cast_clip',
      xml: () => pageXml('cast_clip'), data: async () => ({ clip_url: `${origin}/demo/clip-gray/${Date.now()}.fset` }) },
    { id: 'cast_clip_glasscast', label: 'cast_clip + 本机 glasscast（实验接口 clip.fset，改 data 里的 key）', page: 'cast_clip',
      xml: () => pageXml('cast_clip'), data: async () => ({ clip_url: `${glasscast}/clip.fset` }) },
  ];
  const out = /** @type {Preset[]} */ (list.filter(Boolean));
  // pages/ 下的每个页面（cast-image、cast-emit、cast-fast 等）：template.xml + data.json 原样。
  // data.json 里的地址是占位符，渲染前先改（docs/platform-setup.md §9.2 的步骤用的就是这类预设）。
  for (const p of srv?.pages ?? []) {
    if (p.name === 'cast_clip') continue;
    out.push({
      id: `page:${p.name}`, label: `pages/${p.name}（data.json 原样，里面是占位地址）`, page: p.name,
      xml: () => fetchText(p.template),
      data: async () => (p.data ? JSON.parse(await fetchText(p.data)) : {}),
    });
  }
  // 只有 serve.py 带 --samples DIR 启动时才有（/api/info 的 samples 为空就一个也不加）。
  for (const s of srv?.samples ?? []) {
    const name = decodeURIComponent(s.split('/').pop() || 'sample').replace(/\.xml$/, '');
    out.push({ id: `sample:${name}`, label: `示例 XML：${name}（--samples 目录，data 为空）`, page: name,
      xml: () => fetchText(s), data: async () => ({}) });
  }
  return out;
}

/** @param {string} id */
async function applyPreset(id) {
  const preset = presets.find((p) => p.id === id);
  if (!preset) throw new Error(`未知预设：${id}`);
  const [xml, data] = await Promise.all([preset.xml(), preset.data()]);
  els.xml.value = xml;
  els.data.value = JSON.stringify(data, null, 2);
  els.page.value = preset.page;
  els.preset.value = id;
  refreshUri();
  saveState();
  uiLog('info', `已载入预设：${preset.label}`);
}

// ---- 操作 ----

async function doRender() {
  let data;
  try { data = parseDataOrThrow(); } catch (e) { uiLog('error', errText(e)); setStatus('数据错误', 'err'); return; }
  const xml = els.xml.value;
  if (!xml.trim()) { uiLog('error', 'template.xml 为空'); return; }
  const lint = lintXml(xml);
  if (lint) uiLog('warn', `template.xml 结构有问题（内核可能照样报 render ok，但平台保存/真机未必接受）：${lint}`);
  saveState();
  const uri = currentUri();
  uiLog('info', `渲染 ${uri}（xml ${xml.length} 字符，data ${JSON.stringify(data).length} 字符）`);
  counters.renders += 1;
  try {
    const outcome = await harness.loadTemplate({ xml, data, uri });
    if (outcome === 'rendered') counters.rendered += 1;
    else if (outcome === 'failed') counters.failed += 1;
    else counters.unconfirmed += 1;
  } catch (e) {
    counters.failed += 1;
    uiLog('error', `渲染出错：${errText(e)}`);
    setStatus('渲染出错', 'err');
  }
  scheduleStats();
}

async function doUpdate() {
  let data;
  try { data = parseDataOrThrow(); } catch (e) { uiLog('error', errText(e)); return; }
  saveState();
  try {
    await harness.updateData(data);
    uiLog('info', '已用新数据重注入（不 pop）');
  } catch (e) {
    uiLog('error', errText(e));
  }
}

/** 内核里是否已经设过代理前缀（设过就没法安全撤销，见下）。 */
let proxyApplied = false;

/**
 * 实测：setCorsProxy('') 并不会恢复直连——内核照样把远程地址编码后拼在空前缀后面，
 * XHR 报 "Invalid URL"。所以只在勾选时设置；取消勾选只能重启内核（刷新页面）。
 * @param {boolean} fromToggle
 */
function applyProxySetting(fromToggle) {
  if (!harness.module) return;
  const enable = els.useProxy.checked;
  if (!enable) {
    if (proxyApplied && fromToggle) {
      uiLog('warn', '内核不支持撤销代理前缀，正在重启内核以恢复直连…');
      saveState();
      location.reload();
    }
    return;
  }
  if (!info?.proxy.enabled) {
    uiLog('warn', 'serve.py 没开代理（启动加 --allow-proxy），/proxy 会返回 403');
  }
  try {
    const prefix = new URL('/proxy?url=', location.href).href;
    harness.setCorsProxy(prefix);
    proxyApplied = true;
    uiLog('info', `已设置内核 CORS 代理前缀：${prefix}（对之后发起的下载生效）`);
  } catch (e) {
    uiLog('warn', `设置代理失败：${errText(e)}`);
  }
}

/** @param {boolean} enabled */
function setEngineButtons(enabled) {
  els.render.disabled = !enabled;
  els.update.disabled = !enabled;
  els.close.disabled = !enabled;
  document.querySelectorAll('[data-key]').forEach((b) => { /** @type {HTMLButtonElement} */ (b).disabled = !enabled; });
}

// ---- 事件接线 ----

harness.on((ev) => {
  switch (ev.type) {
    case 'log':
      appendLog(ev.level, ev.source, ev.message);
      break;
    case 'progress':
      setStatus(ev.message.replace('Downloading data...', '下载资源包'), 'warn');
      break;
    case 'error':
      appendLog('error', 'harness', ev.message);
      break;
    case 'status': {
      /** @type {Record<string, [string, ''|'ok'|'warn'|'err']>} */
      const map = {
        booting: ['启动内核…', 'warn'], ready: ['就绪', 'ok'], rendering: ['渲染中…', 'warn'],
        rendered: ['已渲染', 'ok'], failed: ['渲染失败', 'err'], unconfirmed: ['渲染未确认', 'warn'],
        error: ['内核启动失败', 'err'], destroyed: ['已销毁', ''],
      };
      const [text, kind] = map[ev.status] || [ev.status, ''];
      setStatus(ev.detail ? `${text}（${ev.detail}）` : text, kind);
      if (ev.status === 'ready' && harness.module) {
        setEngineButtons(true);
        const bi = harness.module._buildInfo;
        els.build.textContent = bi ? `waft-lite v${bi.version} ${bi.variant}/${bi.channel} wasm=${bi.wasmSha256}` : 'build stamp 不可用';
        applyScreenLayout();
      }
      if (ev.status === 'error') {
        setEngineButtons(false);
        showBanner(`内核启动失败：${ev.detail || ''}\n排查：1) 是否用 serve.py 打开（需要 COOP/COEP）；2) fetch_assets.sh 是否成功；3) 浏览器控制台里 worker / SharedArrayBuffer 报错。`);
      }
      break;
    }
    case 'render':
      counters.lastRenderMs = ev.durationMs;
      uiLog(ev.outcome === 'rendered' ? 'info' : 'warn', `渲染结果：${ev.outcome}（${ev.durationMs} ms）${ev.uri}`);
      break;
    case 'inspect':
      showInspect(ev.report);
      break;
    case 'waftStatus':
      uiLog('info', `waft status [${ev.category}]: ${safeJson(ev.data)}`);
      break;
    case 'uiAction':
      uiLog('info', `uiAction: ${safeJson(ev.action)}`);
      break;
    case 'cameraRequest':
      uiLog('info', `cameraRequest ${ev.action}: ${safeJson(ev.detail)}（模拟器不实现相机）`);
      break;
    default:
      break;
  }
});

/** @param {any} v */
function safeJson(v) {
  try { return JSON.stringify(v); } catch { return String(v); }
}

els.render.addEventListener('click', () => { void doRender(); });
els.update.addEventListener('click', () => { void doUpdate(); });
els.close.addEventListener('click', () => { void harness.closePage().catch((e) => uiLog('error', errText(e))); });
els.reboot.addEventListener('click', () => { saveState(); location.reload(); });
els.loadPreset.addEventListener('click', () => { void applyPreset(els.preset.value).catch((e) => uiLog('error', errText(e))); });
els.app.addEventListener('input', () => { refreshUri(); saveState(); });
els.page.addEventListener('input', () => { refreshUri(); saveState(); });
els.xml.addEventListener('change', saveState);
els.data.addEventListener('change', saveState);
els.useProxy.addEventListener('change', () => { saveState(); applyProxySetting(true); });
els.addLatLng.addEventListener('change', saveState);
els.scale.addEventListener('change', () => { applyScreenLayout(); saveState(); });
els.fullCanvas.addEventListener('change', () => { applyScreenLayout(); saveState(); });
els.inspect.addEventListener('change', () => { applyScreenLayout(); if (!els.inspect.checked) harness.clearInspect(); });
els.logFilter.addEventListener('input', rerenderLog);
els.logImportant.addEventListener('change', rerenderLog);
els.clearLog.addEventListener('click', () => { logLines.length = 0; els.log.replaceChildren(); });
els.copyLog.addEventListener('click', async () => {
  const text = logLines.map((l) => `${(l.t / 1000).toFixed(3)} ${l.level} ${l.source} ${l.message}`).join('\n');
  try { await navigator.clipboard.writeText(text); uiLog('info', `已复制 ${logLines.length} 行`); } catch (e) { uiLog('warn', `复制失败：${errText(e)}`); }
});
document.querySelectorAll('[data-key]').forEach((btn) => {
  btn.addEventListener('click', () => {
    const name = /** @type {keyof typeof KEY_CODES} */ (/** @type {HTMLElement} */ (btn).dataset.key);
    try { harness.sendKey(name); } catch (e) { uiLog('error', errText(e)); }
  });
});
// 点选检查：坐标换算到画布像素（getBoundingClientRect 已含缩放/平移）。
els.screenCrop.addEventListener('click', (ev) => {
  if (!els.inspect.checked || !harness.module) return;
  const rect = els.canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const x = Math.round((ev.clientX - rect.left) * els.canvas.width / rect.width);
  const y = Math.round((ev.clientY - rect.top) * els.canvas.height / rect.height);
  harness.inspectAt(x, y);
});

// ---- 启动 ----

async function init() {
  setEngineButtons(false);
  const saved = loadState();
  if (saved) {
    els.xml.value = saved.xml ?? '';
    els.data.value = saved.data ?? '';
    els.app.value = saved.app || 'glasscast';
    els.page.value = saved.page || 'cast-image';
    if (saved.scale) els.scale.value = saved.scale;
    els.fullCanvas.checked = !!saved.fullCanvas;
    els.useProxy.checked = !!saved.useProxy;
    els.addLatLng.checked = saved.addLatLng !== false;
  }
  if (params.get('scale')) els.scale.value = /** @type {string} */ (params.get('scale'));
  if (params.has('proxy')) els.useProxy.checked = params.get('proxy') === '1';
  applyScreenLayout();
  refreshUri();

  try {
    const res = await fetch('/api/info', { cache: 'no-store' });
    info = res.ok ? await res.json() : null;
  } catch {
    info = null;
  }
  // 拉取记录只看本页打开之后的请求。
  if (info && typeof info.server_time === 'number') lastRequestTs = info.server_time;
  presets = buildPresets(info);
  els.preset.replaceChildren(...presets.map((p) => {
    const o = document.createElement('option');
    o.value = p.id;
    o.textContent = p.label;
    return o;
  }));
  if (saved?.preset && presets.some((p) => p.id === saved.preset)) els.preset.value = saved.preset;
  els.proxyHint.textContent = info?.proxy.enabled
    ? `serve.py 已开代理（${info.proxy.allow_private ? '含本机/内网' : '仅公网目标'}）。glasscast 自带 CORS 头，一般不需要代理。`
    : '代理未开启（serve.py 加 --allow-proxy）。glasscast 和 /demo 都自带 CORS 头，不需要代理。';

  if (!info) {
    showBanner('读不到 /api/info：本页需要由 tools/simulator/serve.py 提供。');
  } else if (!info.assets.ok) {
    showBanner(`内核资源不完整：${[...info.assets.missing, ...Object.keys(info.assets.invalid)].join(', ')}\n先运行：tools/simulator/fetch_assets.sh`);
    setStatus('缺内核资源', 'err');
    void pollRequests();
    return;
  }
  if (!globalThis.crossOriginIsolated) {
    showBanner('页面不是 cross-origin isolated（响应缺 COOP/COEP 头），内核无法启动。请通过 serve.py 访问。');
    setStatus('未隔离', 'err');
    return;
  }

  const presetParam = params.get('preset');
  if (presetParam && !presets.some((p) => p.id === presetParam)) {
    // 例如 sample:<name> 但 serve.py 没带 --samples，或 pages/ 里没有这个页面：提示一下，改载自检。
    uiLog('warn', `没有预设 ${presetParam}（可用：${presets.map((p) => p.id).join('、')}），改为载入内核自检`);
    await applyPreset('selftest').catch((e) => uiLog('error', errText(e)));
  } else if (presetParam) {
    await applyPreset(presetParam).catch((e) => uiLog('error', errText(e)));
  } else if (!els.xml.value.trim()) {
    await applyPreset('selftest').catch((e) => uiLog('error', errText(e)));
  }

  void pollRequests();
  try {
    await harness.boot();
  } catch {
    return; // 错误已经在 status 事件里展示
  }
  applyProxySetting(false);
  try {
    const off = harness.module?.ccall('wasm_get_content_offset_y', 'number', [], []);
    uiLog('info', `wasm_get_content_offset_y() = ${off}（取景顶部固定用 ${SCREEN.top}）`);
  } catch (e) {
    uiLog('warn', `wasm_get_content_offset_y 调用失败：${errText(e)}`);
  }
  if (params.get('autorender') === '1') await doRender();
}

// 给自动化/调试用（浏览器控制台里 glasscastSim.render() 等）。
Object.assign(window, {
  glasscastSim: {
    harness,
    render: doRender,
    update: doUpdate,
    applyPreset,
    getLog: () => logLines.map((l) => `${l.level} ${l.source} ${l.message}`),
    getStats: () => ({ ...counters, fetchTimes: JSON.parse(JSON.stringify(fetchTimes)) }),
    snapshot: () => els.canvas.toDataURL('image/png'),
  },
});

void init();
