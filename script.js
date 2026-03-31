/**
 * Blade RPG：解析 POST /api/turn_stream 回傳的 SSE（data: JSON 行）。
 * onDelta 收到從 JSON narrative 欄位抽出的文字片段；完成後回傳含 game_state 的結尾 payload。
 */
(function (global) {
  global.consumeBladeTurnSse = async function (reader, onDelta) {
    const decoder = new TextDecoder("utf-8");
    let sseBuf = "";
    let streamErr = null;
    let finalPayload = null;

    const flushSseBlocks = () => {
      for (;;) {
        const splitIdx = sseBuf.indexOf("\n\n");
        if (splitIdx < 0) break;
        const block = sseBuf.slice(0, splitIdx);
        sseBuf = sseBuf.slice(splitIdx + 2);
        const lines = block.split("\n");
        for (let li = 0; li < lines.length; li++) {
          const line = lines[li].trim();
          if (!line.startsWith("data:")) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          let obj;
          try {
            obj = JSON.parse(payload);
          } catch (_) {
            continue;
          }
          if (obj.error) {
            streamErr = String(obj.error);
            return;
          }
          if (obj.narrative_delta && typeof onDelta === "function") {
            onDelta(String(obj.narrative_delta));
          }
          if (obj.done) {
            finalPayload = obj;
          }
        }
      }
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        sseBuf += decoder.decode();
        flushSseBlocks();
        break;
      }
      sseBuf += decoder.decode(value, { stream: true });
      flushSseBlocks();
      if (streamErr) break;
    }

    if (streamErr) throw new Error(streamErr);
    if (!finalPayload || !finalPayload.game_state) {
      throw new Error("串流未回傳完整遊戲狀態");
    }
    return finalPayload;
  };
})(typeof window !== "undefined" ? window : globalThis);

/**
 * 屬性彈窗：上帝模式編輯 core_save，POST /api/update_god_mode
 */
(function (global) {
  let cfg = null;
  let editing = false;
  let snapshot = null;

  function el(id) {
    return typeof document !== "undefined" ? document.getElementById(id) : null;
  }

  function defaultCore() {
    return {
      identity: { 名號: "—", 境界: "練氣守一", 目前裝備: "無" },
      inventory: [],
      relationships: {},
      location: "",
      milestones: [],
      summary: "",
    };
  }

  function readCoreFromState(gs) {
    const d = defaultCore();
    const c = gs && gs.core_save;
    if (!c || typeof c !== "object") return d;
    if (c.identity && typeof c.identity === "object") {
      Object.assign(d.identity, c.identity);
    }
    if (Array.isArray(c.inventory)) d.inventory = c.inventory.slice();
    if (c.relationships && typeof c.relationships === "object" && !Array.isArray(c.relationships)) {
      d.relationships = { ...c.relationships };
    }
    if (typeof c.location === "string") d.location = c.location;
    if (Array.isArray(c.milestones)) d.milestones = c.milestones.slice();
    if (typeof c.summary === "string") d.summary = c.summary;
    return d;
  }

  function fillView(core) {
    const vName = el("god-v-name");
    const vRealm = el("god-v-realm");
    const vEquip = el("god-v-equip");
    const vLoc = el("god-v-location");
    const vInv = el("god-v-inventory");
    const vRel = el("god-v-rel");
    const vMs = el("god-v-milestones");
    const vSum = el("god-v-summary");
    if (vName) vName.textContent = core.identity["名號"] || "—";
    if (vRealm) vRealm.textContent = core.identity["境界"] || "—";
    if (vEquip) vEquip.textContent = core.identity["目前裝備"] || "—";
    if (vLoc) vLoc.textContent = core.location.trim() ? core.location : "—";
    if (vInv) vInv.textContent = core.inventory.length ? core.inventory.join("\n") : "（空）";
    if (vRel) {
      vRel.textContent = Object.keys(core.relationships).length
        ? JSON.stringify(core.relationships, null, 2)
        : "（空）";
    }
    if (vMs) vMs.textContent = core.milestones.length ? core.milestones.join("\n") : "（空）";
    if (vSum) vSum.textContent = core.summary.trim() ? core.summary : "（空）";
  }

  function fillEdit(core) {
    const eName = el("god-e-name");
    const eRealm = el("god-e-realm");
    const eEquip = el("god-e-equip");
    const eLoc = el("god-e-location");
    const eInv = el("god-e-inventory");
    const eRel = el("god-e-rel");
    const eMs = el("god-e-milestones");
    const eSum = el("god-e-summary");
    if (eName) eName.value = core.identity["名號"] || "";
    if (eRealm) eRealm.value = core.identity["境界"] || "";
    if (eEquip) eEquip.value = core.identity["目前裝備"] || "";
    if (eLoc) eLoc.value = core.location || "";
    if (eInv) eInv.value = core.inventory.join("\n");
    if (eRel) {
      eRel.value = Object.keys(core.relationships).length
        ? JSON.stringify(core.relationships, null, 2)
        : "{}";
    }
    if (eMs) eMs.value = core.milestones.join("\n");
    if (eSum) eSum.value = core.summary;
  }

  function readEditIntoCore() {
    const identity = {
      名號: (el("god-e-name") && el("god-e-name").value.trim()) || "—",
      境界: (el("god-e-realm") && el("god-e-realm").value.trim()) || "練氣守一",
      目前裝備: (el("god-e-equip") && el("god-e-equip").value.trim()) || "無",
    };
    const invRaw = el("god-e-inventory") ? el("god-e-inventory").value : "";
    const inventory = invRaw
      .split(/\r?\n/)
      .map(function (s) {
        return s.trim();
      })
      .filter(Boolean);
    const relRaw = el("god-e-rel") ? el("god-e-rel").value.trim() : "";
    let relationships = {};
    if (relRaw) {
      const parsed = JSON.parse(relRaw);
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("人際關係必須為 JSON 物件（例如 {\"NPC 名\":\"好感描述\"}）");
      }
      relationships = parsed;
    }
    const msRaw = el("god-e-milestones") ? el("god-e-milestones").value : "";
    const milestones = msRaw
      .split(/\r?\n/)
      .map(function (s) {
        return s.trim();
      })
      .filter(Boolean);
    const location = el("god-e-location") ? el("god-e-location").value.trim() : "";
    const summary = el("god-e-summary") ? el("god-e-summary").value : "";
    return {
      identity: identity,
      inventory: inventory,
      relationships: relationships,
      location: location,
      milestones: milestones,
      summary: summary,
    };
  }

  function setEditing(on) {
    editing = on;
    const modal = cfg ? el(cfg.modalId) : null;
    if (modal) {
      modal.classList.toggle("god-editing", on);
      modal.classList.toggle("god-mode-active", on);
    }
    const editBtn = el("btn-god-mode-edit");
    const row = el("god-mode-action-row");
    if (editBtn) editBtn.classList.toggle("hidden", on);
    if (row) row.classList.toggle("hidden", !on);
  }

  function exitEdit(restoreSnapshot) {
    if (restoreSnapshot && snapshot) {
      fillEdit(snapshot);
    }
    if (!restoreSnapshot) snapshot = null;
    setEditing(false);
  }

  global.BladeGodMode = {
    init: function (c) {
      cfg = c;
      const editBtn = el("btn-god-mode-edit");
      const saveBtn = el("btn-god-mode-save");
      const cancelBtn = el("btn-god-mode-cancel");
      if (!editBtn || !saveBtn || !cancelBtn || !cfg) return;
      editBtn.addEventListener("click", function () {
        const gs = cfg.getGameState();
        if (!gs) {
          cfg.setError("尚未載入遊戲狀態");
          return;
        }
        snapshot = readCoreFromState(gs);
        fillEdit(snapshot);
        setEditing(true);
        cfg.setError("");
      });
      cancelBtn.addEventListener("click", function () {
        exitEdit(true);
      });
      saveBtn.addEventListener("click", async function () {
        if (!cfg) return;
        var payload;
        try {
          payload = readEditIntoCore();
        } catch (e) {
          cfg.setError(String((e && e.message) || e));
          return;
        }
        cfg.setLoading(true);
        cfg.setError("");
        try {
          const res = await fetch(cfg.apiUrl, {
            method: "POST",
            headers: cfg.authHeadersFn(),
            body: JSON.stringify({ core_save: payload }),
          });
          const data = await res.json().catch(function () {
            return {};
          });
          if (res.status === 401) {
            if (typeof cfg.onUnauthorized === "function") cfg.onUnauthorized();
            return;
          }
          if (!res.ok) {
            const d = data.detail;
            const msg = Array.isArray(d)
              ? d
                  .map(function (x) {
                    return x.msg || x;
                  })
                  .join(" ")
              : d || "儲存失敗";
            cfg.setError(msg);
            return;
          }
          if (data.game_state) {
            cfg.applyGameState(data.game_state, {
              animateLastAssistant: !!data.god_reality_whisper,
            });
          }
          const gs2 = data.game_state || cfg.getGameState();
          snapshot = readCoreFromState(gs2);
          fillView(snapshot);
          exitEdit(false);
        } catch (e2) {
          cfg.setError(String((e2 && e2.message) || e2));
        } finally {
          cfg.setLoading(false);
        }
      });
    },
    refresh: function () {
      if (!cfg || editing) return;
      const gs = cfg.getGameState();
      if (!gs) return;
      fillView(readCoreFromState(gs));
    },
    onModalClose: function () {
      if (editing) exitEdit(true);
      const modal = cfg ? el(cfg.modalId) : null;
      if (modal) {
        modal.classList.remove("god-mode-active", "god-editing");
      }
      const eb = el("btn-god-mode-edit");
      const row = el("god-mode-action-row");
      if (eb) eb.classList.remove("hidden");
      if (row) row.classList.add("hidden");
    },
  };
})(typeof window !== "undefined" ? window : globalThis);

/**
 * 劇情框右下角推演標籤：桌機版隨機氛圍文案（由 index.html setStoryInferenceLoading 呼叫）。
 */
(function (global) {
  var PHRASES = [
    "正在結算因果…",
    "正在推演亞空間波動...",
    "正在計算天道劫數...",
    "正在檢索奧林帕斯神諭...",
    "正在校準萬界錨點座標...",
    "正在同步鑄律遠征殘響...",
  ];
  global.BladeCausalSettlement = {
    pickPhrase: function () {
      return PHRASES[Math.floor(Math.random() * PHRASES.length)];
    },
  };
})(typeof window !== "undefined" ? window : globalThis);
