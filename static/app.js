const { createApp } = Vue;

const EventTimeline = {
  props: ["events"],
  computed: {
    rows() {
      return (this.events || [])
        .filter(e => !["arrive_attempt"].includes(e.type))
        .map(e => ({
          ...e,
          label: {
            created: "订单创建（规则快照锁定）", dispatched: "服务员出发",
            arrived: "服务员到场", arrive_attempt: "签到尝试",
            cancelled: "客户取消",
          }[e.type] || e.type,
        }));
    },
  },
  template: `
  <div class="card timeline">
    <h3>服务端事件流（按 seq 判定先后；客户端时间仅留档）</h3>
    <div v-for="e in rows" :key="e.seq" class="ev">
      <span class="seq">#{{e.seq}}</span>
      <span class="ty">{{e.label}}</span>
      <span>{{new Date(e.server_at_ms).toLocaleString('zh-CN')}}</span>
      <span v-if="e.client_at_ms!==null" class="muted">
        客户端声称：{{new Date(e.client_at_ms).toLocaleString('zh-CN')}}（不参与计费）
      </span>
      <span v-if="e.note" class="muted">— {{e.note}}</span>
    </div>
  </div>`,
};

createApp({
  components: { EventTimeline },
  data() {
    return {
      tab: "orders",
      tabs: [
        { id: "orders", name: "订单总览" },
        { id: "cancel", name: "客户取消" },
        { id: "ops", name: "现场操作" },
        { id: "review", name: "客服改判" },
        { id: "ledger", name: "账本对账" },
        { id: "race", name: "临界时序" },
        { id: "rules", name: "收费规则" },
      ],
      orders: [],
      picked: "",
      detail: null,
      preview: null,
      arrivals: [],
      cancellation: null,
      cancMap: {},
      ledger: null,
      rule: null,
      ruleForm: {},
      cancelReason: "",
      reviewReason: "",
      newStage: "arrived",
      stages: [
        { v: "booked", t: "未出发（0%）" },
        { v: "departed", t: "已出发未到场" },
        { v: "arrived", t: "已到场" },
      ],
      confirming: false,
      cancelResult: null,
      lastOp: "",
      reviewMsg: "",
      refundMsg: "",
      race: null,
      raceEvents: [],
      toast: "",
    };
  },
  computed: {
    activeOrders() { return this.orders.filter(o => o.status !== "cancelled"); },
    cancellableOrders() { return this.orders.filter(o => o.status === "cancelled"); },
  },
  watch: {
    tab() { this.loadForTab(); },
    picked() { this.loadForTab(); },
  },
  async mounted() {
    await this.loadOrders();
    this.loadRule();
  },
  methods: {
    yuan(c) { return ((c || 0) / 100).toFixed(2); },
    fmt(ms) { return ms ? new Date(ms).toLocaleString("zh-CN") : "-"; },
    rel(ms) {
      const d = ms - Date.now();
      const mins = Math.round(d / 60000);
      if (Math.abs(mins) < 60) return (mins >= 0 ? mins + " 分钟后" : -mins + " 分钟前");
      return (mins >= 0 ? (mins / 60).toFixed(1) + " 小时后" : (-mins / 60).toFixed(1) + " 小时前");
    },
    statusText(s) { return { booked: "待服务", dispatched: "已出发", arrived: "已到场", cancelled: "已取消" }[s] || s; },
    stageText(s) { return { booked: "未出发", departed: "已出发", arrived: "已到场" }[s] || s; },
    kindText(k) {
      return { payment: "客户付款", cancellation_fee: "空跑费核减", refund: "取消退款",
        additional_charge: "改判补收", additional_refund: "改判追加退款" }[k] || k;
    },
    pretty(o) { return JSON.stringify(o, null, 2); },
    initialFee(code) { return this.cancMap[code]?.original_fee_cents ?? 0; },
    currentFee(code) { return this.cancMap[code]?.current_fee_cents ?? 0; },

    showToast(msg) {
      this.toast = msg;
      clearTimeout(this._t);
      this._t = setTimeout(() => (this.toast = ""), 2600);
    },
    async api(method, url, body) {
      const opt = { method, headers: {} };
      if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
      const res = await fetch(url, opt);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `请求失败 ${res.status}`);
      return data;
    },

    async loadOrders() {
      this.orders = await this.api("GET", "/api/orders");
      this.cancMap = {};
      await Promise.all(this.orders.map(async o => {
        const c = await this.api("GET", `/api/orders/${o.code}/cancellation`);
        if (c) this.cancMap[o.code] = c;
      }));
      if (!this.picked && this.orders.length) this.picked = this.orders[0].code;
      this.loadForTab();
    },
    selectOrder(code) { this.picked = code; this.tab = "ops"; },
    pick(code) { this.picked = code; },

    async loadForTab() {
      this.preview = this.cancellation = this.ledger = this.detail = null;
      this.arrivals = [];
      if (!this.picked) return;
      try {
        if (this.tab === "cancel") { await this.refreshPreview(); }
        if (this.tab === "ops") { await this.loadDetail(); }
        if (this.tab === "review") {
          this.cancellation = await this.api("GET", `/api/orders/${this.picked}/cancellation`);
          this.arrivals = await this.api("GET", `/api/orders/${this.picked}/arrivals`);
        }
        if (this.tab === "ledger") { await this.loadLedger(); }
      } catch (e) { this.showToast(e.message); }
    },

    async refreshPreview() {
      this.cancelResult = null;
      // 每次预览生成订单专属幂等键：同一次取消的重复点击/网络重试只处理一次，
      // 但换订单或重新预览不会复用旧键（服务端也按“已取消”二次去重兜底）。
      this._idem = "web-" + this.picked + "-" + Date.now() + "-" + Math.random().toString(36).slice(2, 8);
      this.preview = await this.api("GET", `/api/orders/${this.picked}/cancel-preview`);
    },
    async doCancel() {
      this.confirming = true;
      try {
        this.cancelResult = await this.api("POST", `/api/orders/${this.picked}/cancel`,
          { reason: this.cancelReason, idempotency_key: this._idem });
        this.preview = await this.api("GET", `/api/orders/${this.picked}/cancel-preview`);
        await this.loadOrders();
        this.showToast("取消已提交");
      } catch (e) { this.showToast(e.message); }
      finally { this.confirming = false; }
    },
    goLedger() { this.tab = "ledger"; },

    async loadDetail() {
      this.detail = await this.api("GET", `/api/orders/${this.picked}`);
      this.arrivals = await this.api("GET", `/api/orders/${this.picked}/arrivals`);
    },
    async doDispatch() {
      try {
        const r = await this.api("POST", `/api/orders/${this.picked}/dispatch`,
          { client_at_ms: Date.now(), note: "服务员点击出发" });
        this.lastOp = `出发已记录：事件 #${r.seq}，服务端时间 ${this.fmt(r.server_at_ms)}`;
        await this.loadOrders();
      } catch (e) { this.showToast(e.message); }
    },
    async doArrive(lat, lng) {
      try {
        const r = await this.api("POST", `/api/orders/${this.picked}/arrive`,
          { lat, lng, client_at_ms: Date.now() });
        this.lastOp = r.accepted_as_arrived
          ? `定位距服务地址 ${r.distance_m}m，围栏内，到场已确认（事件 #${r.checkin_event_seq}）`
          : `定位距服务地址 ${r.distance_m}m，围栏外，未确认为到场，已留签到证据待客服核对`;
        await this.loadOrders();
      } catch (e) { this.showToast(e.message); }
    },

    async doReview() {
      this.reviewMsg = "";
      try {
        await this.api("POST", `/api/orders/${this.picked}/reviews`,
          { new_stage: this.newStage, reason: this.reviewReason, agent: "客服001" });
        this.reviewMsg = "改判已提交：原金额保留，差额已以补收/追加退款单入账，原账未改动。";
        this.reviewReason = "";
        await this.loadOrders();
        this.cancellation = await this.api("GET", `/api/orders/${this.picked}/cancellation`);
      } catch (e) { this.showToast(e.message); }
    },

    async loadLedger() {
      this.refundMsg = "";
      this.ledger = await this.api("GET", `/api/orders/${this.picked}/ledger`);
    },
    async doRefundRetry() {
      try {
        const r = await this.api("POST", `/api/orders/${this.picked}/refund-retry`);
        this.refundMsg = r.message;
        await this.loadLedger();
      } catch (e) { this.showToast(e.message); }
    },

    async runRace() {
      try {
        await this.api("POST", "/api/admin/reseed");
        await this.loadOrders();
        this.picked = "OD-RACE";
        this.race = await this.api("POST", "/api/orders/OD-RACE/race-demo");
        this.raceEvents = (await this.api("GET", "/api/orders/OD-RACE")).events;
        await this.loadOrders();
      } catch (e) { this.showToast(e.message); }
    },

    async loadRule() {
      this.rule = await this.api("GET", "/api/rules/current");
      this.ruleForm = { ...this.rule };
    },
    async saveRule() {
      try {
        await this.api("PUT", "/api/rules", this.ruleForm);
        this.showToast("已保存为新版本，仅影响新订单");
        await this.loadRule();
      } catch (e) { this.showToast(e.message); }
    },

    async reseed() {
      await this.api("POST", "/api/admin/reseed");
      this.race = null;
      this.showToast("演示数据已重置");
      await this.loadOrders();
      this.loadRule();
    },
  },
}).mount("#app");
