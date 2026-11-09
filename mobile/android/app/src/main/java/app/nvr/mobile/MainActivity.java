package app.nvr.mobile;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.WindowManager;

import com.getcapacitor.BridgeActivity;
import com.getcapacitor.CapConfig;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(EnrolledServerPlugin.class);
        super.onCreate(savedInstanceState);
        // Camera pictures stay out of screenshots, screen recordings and the recent-apps view.
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_SECURE, WindowManager.LayoutParams.FLAG_SECURE);
        // The channel the NVR names in each alarm notification.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel("alarms", "Alarms", NotificationManager.IMPORTANCE_HIGH);
            channel.setDescription("Alarms raised by your NVR's rules");
            getSystemService(NotificationManager.class).createNotificationChannel(channel);
        }
    }

    /**
     * The bridge trusts the bundled screen and, once the phone is enrolled, exactly that server's origin (#299): nothing else may call native plugins.
     * The host is listed for navigation, and the full origin so that a server on another port is trusted on that port.
     */
    @Override
    protected void load() {
        String origin = EnrolledServerPlugin.enrolled(this);
        if (origin != null) {
            try (InputStream stream = getAssets().open("capacitor.config.json")) {
                ByteArrayOutputStream bytes = new ByteArrayOutputStream();
                byte[] buffer = new byte[4096];
                for (int read; (read = stream.read(buffer)) > 0; ) bytes.write(buffer, 0, read);
                JSONObject json = new JSONObject(bytes.toString("UTF-8"));
                JSONObject server = json.optJSONObject("server");
                if (server == null) server = new JSONObject();
                server.put("allowNavigation", new JSONArray().put(Uri.parse(origin).getHost()).put(origin));
                json.put("server", server);
                config = new CapConfig(getAssets(), json);
            } catch (Exception failure) {
                // Without the enrolled origin in the configuration the NVR page simply gets no bridge; the start screen still works.
                config = null;
            }
        }
        super.load();
    }
}
