/* 家政服务临时取消与空跑费核算系统 — Vue 3（免构建，本地运行时） */
const { createApp, reactive, computed, onMounted, ref } = Vue;

const API = "";
async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* ignore */ }
  if (!res.ok) {
    const msg = (data && (data.detail?.message || data.detail || data.message)) || `请求失败 ${res.status}`;
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = res.status;
    err.data = data;
    throw err;
  }
  return data;
}

const yuan = (c) => (c == null ? "-" : (c / 100).toFixed(2));
const STATE_LABEL = {
  NOT_DISPATCHED: "服务员尚未出发",
  DISPATCHED: "已出发·未到场",
  ARRIVED: "已到场",
  DISPUTED: "到场争议中",
};
const RESULT_LABEL = {
  FREE_NOT_DISPATCHED: "未出发，免收",
  DISPATCH_FEE: "已出发空跑费",
  ARRIVAL_FEE: "已到场空跑费",
  WAIVED: "客服免除",
  MANUAL: "客服核定",
};
const STATUS_TAG = {
  created: "gray", dispatched: "blue", arrived: "green", cancelled: "red", completed: "gray",
};

createApp({
  setup() {
    const s = reactive({
      role: "customer",
      orders: [],
      currentId: null,
      order: null,
      loading: false,
      toast: null,
      serverNow: null,
      createForm: {
        customer_name: "李女士", service_type: "日常保洁", service_address: "上海市浦东新区示例路 88 号",
        lat: 31.2304, lng: 121.4737, amount_yuan: 300,
        scheduled_in_hours: 3, paid: true,
      },
      checkin: { lat: 31.2304, lng: 121.4737, accuracy_m: 15 },
      cancelReason: "",
      cancelKey: "ck-" + Math.random().toString(36).slice(2, 10),
      preview: null, // 取消预览弹窗数据
      submitting: false,
      disputeNote: "",
      disputeClaims: false,
      adj: { operator: "客服01", reason: "", mode: "not_arrived", amount_yuan: 0 },
      adjust: { amount_yuan: 0, reason: "", operator: "客服01", key: "adj-" + Math.random().toString(36).slice(2, 8) },
    });

    let toastTimer = null;
    function flash(msg, kind = "ok") {
      s.toast = { msg, kind };
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => (s.toast = null), 3200);
    }

    async function loadOrders(selectId) {
      s.orders = await api("/api/orders");
      if (selectId) s.currentId = selectId;
      if (!s.currentId && s.orders.length) s.currentId = s.orders[0].id;
      await loadOrder();
    }
    async function loadOrder() {
      if (!s.currentId) { s.order = null; return; }
      s.order = await api(`/api/orders/${s.currentId}`);
    }
    async function refresh(keepId) {
      await loadOrders(keepId || s.currentId);
    }
    async function refreshClock() {
      try { s.serverNow = await api("/api/dev/clock", { method: "POST", body: {} }); } catch (e) {}
    }

    async function createOrder() {
      const f = s.createForm;
      const scheduledAt = new Date(Date.now() + Number(f.scheduled_in_hours) * 3600 * 1000).toISOString();
      try {
        const o = await api("/api/orders", {
          method: "POST",
          body: {
            customer_name: f.customer_name, service_type: f.service_type,
            service_address: f.service_address, lat: Number(f.lat), lng: Number(f.lng),
            amount_yuan: Number(f.amount_yuan), scheduled_at: scheduledAt,
            paid: f.paid,
          },
        });
        flash("订单已创建，收费规则已锁定为快照 v" + o.rule_version);
        await refresh(o.id);
      } catch (e) { flash(e.message, "err"); }
    }

    async function act(promise, okMsg) {
      try {
        await promise;
        if (okMsg) flash(okMsg);
        await refresh();
        return true;
      } catch (e) {
        flash(e.message, "err");
        return false;
      }
    }

    const dispatch = () =>
      act(api(`/api/orders/${s.currentId}/dispatch`, { method: "POST", body: {} }),
          "已记录服务员出发（服务端事件）");

    async function checkin(within) {
      const c = s.checkin;
      let lat = Number(c.lat), lng = Number(c.lng);
      if (within === false) { lat = Number(c.lat) + 0.01; } // 约 1.1km，落在 200m 围栏外
      await act(
        api(`/api/orders/${s.currentId}/checkin`, {
          method: "POST",
          body: { lat, lng: Number(c.lng), accuracy_m: Number(c.accuracy_m) || null },
        }),
        within ? "签到在围栏内，已判定到场" : "签到在围栏外，已挂起到场争议待客服核对"
      );
    }

    async function raiseDispute() {
      if (!s.disputeNote.trim()) { flash("请填写争议说明", "err"); return; }
      await act(
        api(`/api/orders/${s.currentId}/dispute`, {
          method: "POST",
          body: { claims_arrived: s.disputeClaims, note: s.disputeNote },
        }),
        "到场争议已登记，等待客服核对"
      );
      s.disputeNote = "";
    }

    // —— 取消：先预览（只读），再由客户决定是否提交 ——
    async function openPreview() {
      try {
        s.preview = await api(`/api/orders/${s.currentId}/cancel/preview`);
      } catch (e) { flash(e.message, "err"); }
    }
    function closePreview() { s.preview = null; }

    async function confirmCancel() {
      s.submitting = true;
      try {
        const r = await api(`/api/orders/${s.currentId}/cancel`, {
          method: "POST",
          body: { reason: s.cancelReason || null, idempotency_key: s.cancelKey },
        });
        s.preview = null;
        if (r.disputed) flash("取消已提交；因到场争议，费用为预估，待客服核对后改判", "ok");
        else if (r.settle && r.settle.ok === false) flash("取消已记录，但结算失败，请重试", "err");
        else flash("取消已提交，费用按服务端事件顺序与锁定规则结算");
        await refresh();
      } catch (e) {
        if (e.status === 402) flash("取消已记录，但支付/退款失败：" + (e.data?.detail || "") + "，请点“重试结算”", "err");
        else flash(e.message, "err");
        s.preview = null;
        await refresh();
      } finally { s.submitting = false; }
    }

    // —— 客服改判 ——
    async function adjudicate() {
      if (!s.adj.reason.trim()) { flash("改判必须填写理由（双方可见）", "err"); return; }
      const body = { operator: s.adj.operator, reason: s.adj.reason };
      if (s.adj.mode === "arrived") body.arrival_confirmed = true;
      else if (s.adj.mode === "not_arrived") body.arrival_confirmed = false;
      else if (s.adj.mode === "not_dispatched") body.target_state = "NOT_DISPATCHED";
      else if (s.adj.mode === "dispatched") body.target_state = "DISPATCHED";
      else if (s.adj.mode === "arrived_state") body.target_state = "ARRIVED";
      else if (s.adj.mode === "waive") body.waive = true;
      else if (s.adj.mode === "manual") body.amount_yuan = Number(s.adj.amount_yuan);
      try {
        await api(`/api/orders/${s.currentId}/adjudicate`, { method: "POST", body });
        flash("改判已生效：原金额保留，差额仅以追加调整单找平");
        s.adj.reason = "";
        await refresh();
      } catch (e) {
        if (e.status === 402) {
          flash("改判已记录，但差额结算失败：" + (e.data?.detail || "") + "，请重试", "err");
          await refresh();
        } else flash(e.message, "err");
      }
    }

    const lock = () =>
      act(api(`/api/orders/${s.currentId}/lock`, { method: "POST" }), "账单已锁定：原账冻结，仅可追加补收/退款单");

    async function appendAdjustment() {
      if (!s.adjust.reason.trim()) { flash("请填写追加单理由", "err"); return; }
      try {
        await api(`/api/orders/${s.currentId}/adjustments`, {
          method: "POST",
          body: {
            amount_yuan: Number(s.adjust.amount_yuan), reason: s.adjust.reason,
            operator: s.adjust.operator, idempotency_key: s.adjust.key,
          },
        });
        flash("已追加调整单（原账未改动）");
        s.adjust.reason = ""; s.adjust.amount_yuan = 0;
        await refresh();
      } catch (e) { flash(e.message, "err"); }
    }

    async function retrySettlement() {
      try {
        await api(`/api/orders/${s.currentId}/retry-settlement`, { method: "POST" });
        flash("重试完成；幂等保证未重复收费/退款");
        await refresh();
      } catch (e) {
        if (e.status === 402) flash("重试后仍失败：" + (e.data?.detail || ""), "err");
        else flash(e.message, "err");
        await refresh();
      }
    }

    // —— 演示控制 ——
    const devAdvance = (sec) =>
      act(api("/api/dev/clock", { method: "POST", body: { advance_seconds: sec } }).then(refreshClock),
          `服务端时钟已拨快 ${sec / 3600 >= 1 ? sec / 3600 + " 小时" : sec + " 秒"}`);
    async function injectFail(sub) {
      await act(api("/api/dev/payment/fail", { method: "POST", body: { key_substring: sub || ":cancel:", times: 1 } }),
                "已注入一次支付/退款故障（下次结算触发，再重试即成功）");
    }
    const resetSim = () => act(api("/api/dev/payment/reset", { method: "POST" }), "模拟故障已清除");

    const c = computed(() => s.order);
    const canCancel = computed(() => s.order && s.order.status !== "cancelled" && s.order.status !== "completed");
    const settled = computed(() => s.order?.cancellation?.settle_status === "settled");
    const hasFailedSettle = computed(() => {
      const cn = s.order?.cancellation;
      if (!cn) return false;
      if (cn.settle_status !== "settled") return true;
      return cn.adjudications.some(a => a.settle_status !== "settled");
    });

    onMounted(async () => {
      await refreshClock();
      try { await loadOrders(); } catch (e) { flash(e.message, "err"); }
    });

    return {
      s, c, canCancel, settled, hasFailedSettle,
      yuan, STATE_LABEL, RESULT_LABEL, STATUS_TAG,
      flash, loadOrder, refresh, createOrder, dispatch, checkin, raiseDispute,
      openPreview, closePreview, confirmCancel, adjudicate, lock, appendAdjustment,
      retrySettlement, devAdvance, injectFail, resetSim, refreshClock,
    };
  },

  template: `
  <div>
    <div class="topbar">
      <div>
        <h1>家政服务临时取消与空跑费核算系统</h1>
        <div class="sub">规则快照 · 服务端事件顺序 · 客服改判留痕 · 只追加账本 · 支付幂等</div>
      </div>
      <div class="spacer"></div>
      <div class="sub mono" v-if="s.serverNow">服务端时间：{{ new Date(s.serverNow.now*1000).toLocaleString() }}</div>
      <div class="role-toggle">
        <button :class="{active: s.role==='customer'}" @click="s.role='customer'">客户视角</button>
        <button :class="{active: s.role==='attendant'}" @click="s.role='attendant'">服务员</button>
        <button :class="{active: s.role==='cs'}" @click="s.role='cs'">客服</button>
      </div>
    </div>

    <div class="layout">
      <!-- 左栏 -->
      <div>
        <div class="panel">
          <h2>新建订单 <span class="hint">下单即锁定收费规则快照</span></h2>
          <div class="field"><label>客户</label><input v-model="s.createForm.customer_name" /></div>
          <div class="field"><label>服务项目</label><input v-model="s.createForm.service_type" /></div>
          <div class="field"><label>服务地址</label><input v-model="s.createForm.service_address" /></div>
          <div class="row2">
            <div class="field"><label>纬度 lat</label><input v-model="s.createForm.lat" /></div>
            <div class="field"><label>经度 lng</label><input v-model="s.createForm.lng" /></div>
          </div>
          <div class="row2">
            <div class="field"><label>服务费（元）</label><input type="number" v-model="s.createForm.amount_yuan" /></div>
            <div class="field"><label>距开始（小时）</label><input type="number" v-model="s.createForm.scheduled_in_hours" /></div>
          </div>
          <div class="field">
            <label><input type="checkbox" v-model="s.createForm.paid" /> 已在线预付（金额=服务费）</label>
          </div>
          <button class="btn" @click="createOrder">创建订单</button>
        </div>

        <div class="panel">
          <h2>订单列表</h2>
          <div v-if="!s.orders.length" class="dim">暂无订单</div>
          <div v-for="o in s.orders" :key="o.id" class="order-item"
               :class="{active: o.id===s.currentId}" @click="s.currentId=o.id; loadOrder()">
            <div class="code">{{ o.code }}
              <span class="tag" :class="STATUS_TAG[o.status]">{{
                {created:'待出发',dispatched:'已出发',arrived:'已到场',cancelled:'已取消',completed:'已完成'}[o.status] }}</span>
              <span v-if="o.arrival_disputed" class="tag amber">争议</span>
            </div>
            <div class="meta"><span>{{ o.customer_name }} · {{ o.service_type }}</span>
              <span class="mono">¥{{ o.service_amount_yuan }}{{ o.paid ? ' · 已付' : '' }}</span></div>
          </div>
        </div>

        <div class="panel">
          <h2>演示控制 <span class="hint">模拟时钟 / 支付故障</span></h2>
          <div class="btnrow">
            <button class="btn ghost small" @click="devAdvance(60)">拨快 1 分钟</button>
            <button class="btn ghost small" @click="devAdvance(1800)">拨快 30 分钟</button>
            <button class="btn ghost small" @click="devAdvance(7200)">拨快 2 小时</button>
          </div>
          <div class="btnrow">
            <button class="btn danger small" @click="injectFail(':cancel:')">注入取消结算故障</button>
            <button class="btn danger small" @click="injectFail('adjudication')">注入改判结算故障</button>
            <button class="btn ghost small" @click="resetSim()">清除故障</button>
          </div>
          <div class="section-note">用于验证：退款失败后“重试”只处理一次，金额与原订单仍对得上。</div>
        </div>
      </div>

      <!-- 右栏：订单详情 -->
      <div v-if="c">
        <!-- 基本信息 -->
        <div class="panel">
          <h2>订单 {{ c.code }}
            <span class="tag" :class="STATUS_TAG[c.status]">{{
              {created:'待出发',dispatched:'已出发',arrived:'已到场',cancelled:'已取消',completed:'已完成'}[c.status] }}</span>
            <span v-if="c.arrival_disputed" class="tag amber">到场争议中</span>
            <span class="tag blue">规则快照 v{{ c.rule_version }}</span>
            <span class="tag" :class="c.paid?'green':'gray'">{{ c.paid ? '已预付 ¥'+c.prepaid_yuan : '未预付' }}</span>
          </h2>
          <dl class="kv">
            <dt>客户 / 项目</dt><dd>{{ c.customer_name }} · {{ c.service_type }}</dd>
            <dt>服务地址</dt><dd>{{ c.service_address }}（{{ c.service_lat }}, {{ c.service_lng }}）</dd>
            <dt>服务费</dt><dd class="mono">¥{{ c.service_amount_yuan }}</dd>
            <dt>计划开始</dt><dd>{{ c.scheduled_start }}</dd>
            <dt>当前状态</dt><dd>{{ STATE_LABEL[c.current_state] }}</dd>
          </dl>

          <div class="btnrow">
            <template v-if="s.role==='attendant'">
              <button class="btn secondary" :disabled="!['created'].includes(c.status)" @click="dispatch()">服务员出发</button>
              <button class="btn secondary" @click="checkin(true)">围栏内签到（到场）</button>
              <button class="btn secondary" @click="checkin(false)">围栏外签到（触发争议）</button>
            </template>
            <template v-if="s.role==='customer'">
              <button class="btn danger" :disabled="!canCancel" @click="openPreview()">取消订单</button>
            </template>
            <template v-if="s.role==='cs'">
              <button class="btn ghost small" :disabled="hasFailedSettle===false" @click="retrySettlement()">重试结算</button>
            </template>
          </div>
          <div v-if="s.role==='attendant'" class="section-note">
            签到定位（围栏半径 200m）：
            <input style="width:120px" v-model="s.checkin.lat" />,
            <input style="width:120px" v-model="s.checkin.lng" />
          </div>
        </div>

        <!-- 事件时间线 -->
        <div class="panel">
          <h2>服务端事件时间线 <span class="hint">先后只按 seq / 服务端时间判定，客户端时间仅留档不参与计费</span></h2>
          <table class="tl">
            <thead><tr><th>#</th><th>事件</th><th>来源</th><th>服务端时间（判定依据）</th><th>客户端时间（仅记录）</th><th>备注</th></tr></thead>
            <tbody>
              <tr v-for="e in c.events" :key="e.seq">
                <td class="mono">{{ e.seq }}</td>
                <td>{{
                  {order_created:'下单',attendant_dispatched:'服务员出发',attendant_checkin:'服务员签到',
                   attendant_arrived:'判定到场',arrival_disputed:'到场争议',customer_cancelled:'客户取消',
                   order_adjudicated:'客服改判',account_locked:'锁账',manual_adjustment_appended:'追加调整单'}[e.type] || e.type }}</td>
                <td>{{ {customer:'客户',attendant:'服务员',cs:'客服',system:'系统'}[e.actor] }}</td>
                <td class="mono">{{ e.server_time }}</td>
                <td class="mono" :class="e.client_time ? '' : 'dim'">{{ e.client_time || '—' }}</td>
                <td class="dim">{{ e.note || '' }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 到场证据 -->
        <div class="panel" v-if="c.evidence.length">
          <h2>签到与定位证据 <span class="hint">客服核对“是否到场”</span></h2>
          <table class="tl">
            <thead><tr><th>类型</th><th>时间</th><th>定位</th><th>距服务点</th><th>围栏内</th><th>主张</th><th>说明</th></tr></thead>
            <tbody>
              <tr v-for="(ev,i) in c.evidence" :key="i">
                <td>{{ {attendant_checkin:'服务员签到',customer_claim:'客户主张',cs_note:'客服备注'}[ev.kind] }}</td>
                <td class="mono">{{ ev.server_time }}</td>
                <td class="mono dim">{{ ev.lat ?? '—' }}, {{ ev.lng ?? '—' }}</td>
                <td class="mono">{{ ev.distance_m==null ? '—' : ev.distance_m+' m' }}</td>
                <td><span v-if="ev.within_geofence===null">—</span>
                    <span class="tag green" v-else-if="ev.within_geofence">是</span>
                    <span class="tag red" v-else>否</span></td>
                <td>{{ ev.claims_arrived ? '主张已到场' : '否认到场' }}</td>
                <td class="dim">{{ ev.note }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 客户：争议登记 -->
        <div class="panel" v-if="s.role==='customer' && c.status!=='cancelled'">
          <h2>对“是否到场”有异议？</h2>
          <div class="field">
            <label><input type="radio" :value="true" v-model="s.disputeClaims" /> 我认为服务员已到场</label>
            <label style="margin-left:14px"><input type="radio" :value="false" v-model="s.disputeClaims" /> 我认为服务员未到场</label>
          </div>
          <div class="field"><textarea v-model="s.disputeNote" placeholder="说明情况，客服将核对签到与定位"></textarea></div>
          <button class="btn ghost" @click="raiseDispute">提交争议</button>
        </div>

        <!-- 取消结果与改判（双方可见） -->
        <div class="panel" v-if="c.cancellation">
          <h2>取消费用与客服改判
            <span class="tag" :class="c.cancellation.locked?'purple':(c.cancellation.settle_status==='settled'?'green':'amber')">
              {{ c.cancellation.locked ? '已锁账' : (c.cancellation.settle_status==='settled' ? '已结算' : (c.cancellation.settle_status==='pending'?'待判定':'结算失败')) }}
            </span>
          </h2>

          <div class="row2">
            <div class="adj-card">
              <div class="dim">系统原判定（按锁定规则，永不改变）</div>
              <div class="head">
                <span class="tag gray">{{ RESULT_LABEL[c.cancellation.original.result] || c.cancellation.original.result }}</span>
                <span class="big mono">¥{{ c.cancellation.original.amount_yuan }}</span>
              </div>
              <ul class="basis" style="margin-top:8px">
                <li v-for="(b,i) in c.cancellation.original.basis" :key="i" :class="{dispute: b.k==='dispute'}">
                  <div class="l">{{ b.label }}</div><div class="t">{{ b.text }}</div>
                </li>
              </ul>
            </div>
            <div class="adj-card">
              <div class="dim">最终结果（客户实际承担）</div>
              <div class="head">
                <span class="tag blue">{{ RESULT_LABEL[c.cancellation.final.result] || c.cancellation.final.result }}</span>
                <span class="big mono">¥{{ c.cancellation.final.amount_yuan }}</span>
              </div>
              <div class="section-note" style="margin-top:8px">
                取消时状态：{{ STATE_LABEL[c.cancellation.state_at_cancel] }}；
                取消请求服务端时间：{{ c.cancellation.requested_server_time }}
              </div>
              <div v-if="c.cancellation.settle_status!=='settled'" class="tag red">
                {{ c.cancellation.settle_status==='pending' ? '到场争议待客服判定，暂不结算' : ('结算失败：'+c.cancellation.settle_error) }}
              </div>
            </div>
          </div>

          <!-- 改判记录：双方可见 -->
          <div v-if="c.cancellation.adjudications.length">
            <div class="divider"></div>
            <h2 style="margin-bottom:8px">客服改判记录 <span class="hint">保留原金额、调整结果与理由</span></h2>
            <div class="adj-card" v-for="a in c.cancellation.adjudications" :key="a.seq">
              <div class="head">
                <span>第 {{ a.seq }} 次 · {{ a.operator }} · <span class="dim">{{ a.created_at }}</span></span>
                <span class="tag" :class="a.settle_status==='settled'?'green':'red'">
                  {{ a.settle_status==='settled' ? '差额已找平' : '差额结算失败' }}</span>
              </div>
              <div style="margin:6px 0">
                <span class="mono">¥{{ a.from_amount_yuan }}（{{ RESULT_LABEL[a.from_result]||a.from_result }}）</span>
                <span class="arrow"> → </span>
                <span class="mono">¥{{ a.to_amount_yuan }}（{{ RESULT_LABEL[a.to_result]||a.to_result }}）</span>
              </div>
              <div class="dim">理由：{{ a.reason }}</div>
            </div>
          </div>

          <!-- 客服操作区 -->
          <template v-if="s.role==='cs'">
            <div class="divider"></div>
            <div v-if="!c.cancellation.locked">
              <h2>客服改判 <span class="hint">原金额保留；已结账则只追加补收/退款单找平</span></h2>
              <div class="row2">
                <div class="field">
                  <label>判定结论</label>
                  <select v-model="s.adj.mode">
                    <option value="arrived">确认已到场（80% 空跑费）</option>
                    <option value="not_arrived">确认未到场（已出发 30%）</option>
                    <option value="not_dispatched">改判为未出发（免收）</option>
                    <option value="dispatched">改判为已出发（30%）</option>
                    <option value="arrived_state">改判为已到场（80%）</option>
                    <option value="waive">免除全部空跑费（¥0）</option>
                    <option value="manual">手动核定金额</option>
                  </select>
                </div>
                <div class="field" v-if="s.adj.mode==='manual'">
                  <label>核定金额（元）</label>
                  <input type="number" v-model="s.adj.amount_yuan" />
                </div>
              </div>
              <div class="field"><label>改判理由（必填，客户可查看）</label>
                <textarea v-model="s.adj.reason" placeholder="例如：核对签到定位距服务点 35m，确在围栏内，认定已到场"></textarea></div>
              <button class="btn" @click="adjudicate">提交改判</button>
            </div>

            <div v-else style="margin-top:10px">
              <h2>锁账后追加单 <span class="hint">不能改原账，仅可补收(+)/退款(-)</span></h2>
              <div class="row2">
                <div class="field"><label>金额（元，正=补收 / 负=退款）</label>
                  <input type="number" v-model="s.adjust.amount_yuan" /></div>
                <div class="field"><label>理由（必填）</label><input v-model="s.adjust.reason" /></div>
              </div>
              <button class="btn secondary" @click="appendAdjustment">追加补收/退款单</button>
            </div>

            <div class="btnrow" style="margin-top:10px">
              <button class="btn ghost" :disabled="c.cancellation.locked || hasFailedSettle" @click="lock()">锁定账单</button>
              <button class="btn ghost" :disabled="!hasFailedSettle" @click="retrySettlement()">重试失败结算</button>
            </div>
          </template>
        </div>

        <!-- 账本与对账 -->
        <div class="panel" v-if="c.ledger.entries.length">
          <h2>账本流水（只追加，不可改）与对账</h2>
          <div class="balance-box">
            <div>已收 <span class="big mono">¥{{ yuan(c.ledger.totals.charge_cents) }}</span></div>
            <div class="dim">+</div>
            <div>补收 <span class="big mono">¥{{ yuan(c.ledger.totals.surcharge_cents) }}</span></div>
            <div class="dim">−</div>
            <div>退款 <span class="big mono">¥{{ yuan(c.ledger.totals.refund_cents) }}</span></div>
            <div class="dim">=</div>
            <div>客户净支出 <span class="big mono">¥{{ c.ledger.totals.net_paid_yuan }}</span></div>
            <span class="tag" :class="c.ledger.totals.balanced?'green':'red'">
              {{ c.ledger.totals.balanced ? '对账一致' : '对账不平' }}
              （应为 ¥{{ c.ledger.totals.expected_net_yuan }}）
            </span>
          </div>
          <table class="ledger" style="margin-top:10px">
            <thead><tr><th>类型</th><th>环节</th><th class="right">金额</th><th>交易号</th><th>理由 / 操作人</th><th>时间</th></tr></thead>
            <tbody>
              <tr v-for="e in c.ledger.entries" :key="e.id">
                <td><span class="tag" :class="{CHARGE:'green',FEE:'blue',REFUND:'amber',SURCHARGE:'purple'}[e.entry_type]">
                  {{ {CHARGE:'收款',FEE:'扣费',REFUND:'退款',SURCHARGE:'补收'}[e.entry_type] }}</span></td>
                <td>{{ {prepay:'预付款',cancel_fee_from_prepay:'取消扣费',cancel_refund:'取消退款',
                        cancel_settlement:'取消收款',adjudication_adjust:'改判找平',manual_adjust:'锁后追加'}[e.stage] }}</td>
                <td class="right mono">{{ {REFUND:'−',SURCHARGE:'+'}[e.entry_type]||'' }}¥{{ e.amount_yuan }}</td>
                <td class="dim mono">{{ e.payment_ref }}</td>
                <td class="dim">{{ e.reason }}<span v-if="e.operator"> · {{ e.operator }}</span></td>
                <td class="dim mono">{{ e.created_at }}</td>
              </tr>
            </tbody>
          </table>
          <div class="section-note" v-if="c.ledger.totals.fee_alloc_cents">
            其中“扣费”¥{{ yuan(c.ledger.totals.fee_alloc_cents) }} 为预付款内部划扣，不重复计入现金流入。
          </div>
        </div>
      </div>

      <div class="panel" v-else><div class="dim">请从左侧选择或创建一个订单。</div></div>
    </div>

    <!-- 取消预览弹窗：先看费用与依据，再决定是否提交 -->
    <div class="modal-mask" v-if="s.preview" @click.self="closePreview">
      <div class="modal">
        <h3>取消费用预览</h3>
        <div class="section-note">请先核对可能产生的空跑费与计算依据，再决定是否提交取消。提交后按服务端事件顺序与下单锁定规则结算。</div>

        <template v-if="s.preview.already_cancelled">
          <div class="fee-hero">
            <div class="dim">该订单已取消，费用已生效</div>
            <div class="amount">¥{{ s.preview.fee.amount_yuan }}</div>
            <div>{{ s.preview.message }}</div>
          </div>
        </template>
        <template v-else>
          <div class="fee-hero" :class="{free: s.preview.fee.amount_cents===0, warn: s.preview.fee.provisional}">
            <div class="dim">预计{{ s.preview.fee.provisional ? '（争议保守预估）' : '' }}空跑费</div>
            <div class="amount">¥{{ s.preview.fee.amount_yuan }}</div>
            <div>{{ s.preview.fee.state_label }}</div>
          </div>
          <ul class="basis">
            <li v-for="(b,i) in s.preview.fee.basis" :key="i" :class="{dispute: b.k==='dispute'}">
              <div class="l">{{ b.label }}</div><div class="t">{{ b.text }}</div>
            </li>
          </ul>
          <div class="field" style="margin-top:10px">
            <label>取消原因（可选）</label>
            <textarea v-model="s.cancelReason" placeholder="临时有事，需要取消"></textarea>
          </div>
        </template>

        <div class="btnrow" style="justify-content:flex-end">
          <button class="btn ghost" @click="closePreview">再想想</button>
          <button class="btn danger" :disabled="s.submitting || s.preview.already_cancelled" @click="confirmCancel">
            我已知晓费用，确认提交取消
          </button>
        </div>
      </div>
    </div>

    <div class="toast" v-if="s.toast" :class="s.toast.kind">{{ s.toast.msg }}</div>
  </div>
  `,
}).mount("#app");
