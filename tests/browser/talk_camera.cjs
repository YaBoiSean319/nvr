// A camera's RTSP server with an ONVIF audio backchannel for browser journeys (#147): answers DESCRIBE/SETUP/PLAY and records the RTP packets sent to its speaker,
// so a journey can check that audio reached the camera while the talk control was held and stopped when it was released.
const net = require("node:net");

const REQUIRE = "www.onvif.org/ver20/backchannel";

async function startTalkCamera() {
  const packets = [];
  const state = { teardowns: 0, describes: 0 };
  const sockets = new Set();
  const server = net.createServer(socket => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    socket.on("error", () => undefined);
    let buffer = Buffer.alloc(0);
    let playing = false;
    const reply = (cseq, status, headers = {}, body = "") => {
      const lines = [`RTSP/1.0 ${status}`, `CSeq: ${cseq}`, ...Object.entries(headers).map(([key, value]) => `${key}: ${value}`)];
      if (body) lines.push("Content-Type: application/sdp", `Content-Length: ${Buffer.byteLength(body)}`);
      socket.write(`${lines.join("\r\n")}\r\n\r\n${body}`);
    };
    socket.on("data", chunk => {
      buffer = Buffer.concat([buffer, chunk]);
      for (;;) {
        if (buffer.length === 0) return;
        if (buffer[0] === 0x24) { // An interleaved packet: $ channel length(2) data
          if (buffer.length < 4) return;
          const length = buffer.readUInt16BE(2);
          if (buffer.length < 4 + length) return;
          if (playing && buffer[1] === 0 && length >= 12) packets.push({ payloadType: buffer[5] & 0x7f, sequence: buffer.readUInt16BE(6), bytes: length - 12, at: Date.now() });
          buffer = buffer.subarray(4 + length);
          continue;
        }
        const end = buffer.indexOf("\r\n\r\n");
        if (end < 0) return;
        const head = buffer.subarray(0, end).toString("latin1").split("\r\n");
        buffer = buffer.subarray(end + 4);
        const method = head[0].split(" ")[0];
        const headers = Object.fromEntries(head.slice(1).map(line => [line.slice(0, line.indexOf(":")).trim().toLowerCase(), line.slice(line.indexOf(":") + 1).trim()]));
        const cseq = headers.cseq ?? "0";
        if (method === "OPTIONS") reply(cseq, "200 OK", { Public: "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN" });
        else if (method === "DESCRIBE") {
          state.describes += 1;
          const sdp = ["v=0", "o=- 0 0 IN IP4 127.0.0.1", "s=Camera", "t=0 0", "m=video 0 RTP/AVP 96", "a=rtpmap:96 H264/90000", "a=recvonly", "a=control:track1",
            ...(headers.require?.includes(REQUIRE) ? ["m=audio 0 RTP/AVP 0", "a=rtpmap:0 PCMU/8000", "a=sendonly", "a=control:track2"] : [])].join("\r\n") + "\r\n";
          reply(cseq, "200 OK", { "Content-Base": `rtsp://127.0.0.1:${server.address().port}/stream/` }, sdp);
        } else if (method === "SETUP") reply(cseq, "200 OK", { Session: "AB12;timeout=60", Transport: "RTP/AVP/TCP;unicast;interleaved=0-1" });
        else if (method === "PLAY") { playing = true; reply(cseq, "200 OK", { Session: "AB12" }); }
        else if (method === "TEARDOWN") { state.teardowns += 1; reply(cseq, "200 OK"); socket.end(); }
        else reply(cseq, "405 Method Not Allowed");
      }
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  const url = `rtsp://127.0.0.1:${server.address().port}/stream`;
  return {
    url, packets, state,
    close: () => new Promise(resolve => { for (const socket of sockets) socket.destroy(); server.close(() => resolve()); }),
  };
}

module.exports = { startTalkCamera };
