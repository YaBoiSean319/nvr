package app.nvr.mobile;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.os.Build;
import android.os.Bundle;
import android.view.WindowManager;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {
    @Override
    public void onCreate(Bundle savedInstanceState) {
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
}
