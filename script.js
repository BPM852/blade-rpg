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
