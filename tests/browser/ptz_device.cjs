// An ONVIF camera with a PTZ service for browser journeys: answers what the NVR asks and records
// every PTZ command, so a journey can check what actually reached the camera.
const http = require("node:http");

const ENVELOPE = body => '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:tds="http://www.onvif.org/ver10/device/wsdl" '
  + 'xmlns:trt="http://www.onvif.org/ver10/media/wsdl" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" '
  + `xmlns:tt="http://www.onvif.org/ver10/schema"><s:Body>${body}</s:Body></s:Envelope>`;
const value = (body, tag) => (new RegExp(`<(?:\\w+:)?${tag}>([^<]*)</(?:\\w+:)?${tag}>`).exec(body) || [])[1];
const attributes = (body, tag) => {
  const match = new RegExp(`<(?:\\w+:)?${tag} ([^>]*)/>`).exec(body);
  return match ? Object.fromEntries([...match[1].matchAll(/(\w+)="([^"]*)"/g)].map(item => [item[1], item[2]])) : null;
};

async function startPtzDevice() {
  const commands = [];
  let presets = 0;
  let origin = "";
  const server = http.createServer((request, response) => {
    let body = "";
    request.on("data", chunk => { body += chunk; });
    request.on("end", () => {
      const operation = (/<s:Body><(?:\w+:)?(\w+)/.exec(body) || [])[1];
      let reply = null;
      if (operation === "GetCapabilities") {
        reply = `<tds:GetCapabilitiesResponse><tds:Capabilities><tt:Media><tt:XAddr>${origin}/onvif/media</tt:XAddr></tt:Media><tt:PTZ><tt:XAddr>${origin}/onvif/ptz</tt:XAddr></tt:PTZ></tds:Capabilities></tds:GetCapabilitiesResponse>`;
      } else if (operation === "GetProfiles") {
        reply = '<trt:GetProfilesResponse><trt:Profiles token="profile-main"><tt:Name>Main</tt:Name></trt:Profiles></trt:GetProfilesResponse>';
      } else if (operation === "GetNodes") {
        reply = '<tptz:GetNodesResponse><tptz:PTZNode token="node-1"><tt:SupportedPTZSpaces>'
          + "<tt:ContinuousPanTiltVelocitySpace><tt:URI>x</tt:URI></tt:ContinuousPanTiltVelocitySpace>"
          + "<tt:ContinuousZoomVelocitySpace><tt:URI>x</tt:URI></tt:ContinuousZoomVelocitySpace>"
          + "<tt:RelativePanTiltTranslationSpace><tt:URI>http://www.onvif.org/ver10/tptz/PanTiltSpaces/TranslationSpaceFov</tt:URI></tt:RelativePanTiltTranslationSpace>"
          + "</tt:SupportedPTZSpaces><tt:MaximumNumberOfPresets>64</tt:MaximumNumberOfPresets></tptz:PTZNode></tptz:GetNodesResponse>";
      } else if (["ContinuousMove", "Stop", "RelativeMove", "GotoPreset", "RemovePreset"].includes(operation)) {
        commands.push({ op: operation, pan_tilt: attributes(body, "PanTilt"), zoom: attributes(body, "Zoom"), token: value(body, "PresetToken") });
        reply = `<tptz:${operation}Response/>`;
      } else if (operation === "SetPreset") {
        presets += 1;
        const token = `preset-${presets}`;
        commands.push({ op: "SetPreset", name: value(body, "PresetName"), token });
        reply = `<tptz:SetPresetResponse><tptz:PresetToken>${token}</tptz:PresetToken></tptz:SetPresetResponse>`;
      }
      if (reply === null) {
        response.writeHead(400).end();
        return;
      }
      response.writeHead(200, { "Content-Type": "application/soap+xml" }).end(ENVELOPE(reply));
    });
  });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  return { url: `${origin}/onvif/device_service`, commands, ops: () => commands.map(command => command.op), close: () => new Promise(resolve => server.close(resolve)) };
}

module.exports = { startPtzDevice };
