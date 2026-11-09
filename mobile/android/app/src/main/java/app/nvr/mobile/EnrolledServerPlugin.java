package app.nvr.mobile;

import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ApplicationInfo;
import android.net.Uri;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.util.Arrays;
import java.util.List;
import java.util.Locale;

/**
 * The one server this phone is enrolled with (#299).
 *
 * The address is kept here, in storage only native code writes, and only the bundled start screen may change it. Everything else the
 * privileged WebView is asked to load is handed to the system browser, so a page that is not the enrolled NVR (an external link, a redirect,
 * a rewritten saved address) never runs in the view that can reach the native plugins and the Keystore-backed secrets.
 * The bridge itself trusts only the origins {@link MainActivity} builds into its configuration from this same record.
 */
@CapacitorPlugin(name = "EnrolledServer")
public class EnrolledServerPlugin extends Plugin {
    private static final String FILE = "nvr_enrolment";
    private static final String KEY = "origin";
    /** Sign-in pages of the identity provider the NVR supports. They load as ordinary websites: the bridge is not offered to them. */
    private static final List<String> IDENTITY_PROVIDER_HOSTS = Arrays.asList("login.microsoftonline.com", "login.live.com");

    /** The saved origin, or null when this phone is not enrolled. */
    static String enrolled(Context context) {
        return context.getSharedPreferences(FILE, Context.MODE_PRIVATE).getString(KEY, null);
    }

    static boolean debuggable(Context context) {
        return (context.getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0;
    }

    /** scheme://host[:port] for an address, or null when it is not one this app will enrol: https, or http only in a development build. */
    static String normalise(String address, boolean allowCleartext) {
        if (address == null || address.length() > 300) return null;
        Uri uri = Uri.parse(address.trim());
        String scheme = uri.getScheme() == null ? "" : uri.getScheme().toLowerCase(Locale.ROOT);
        String host = uri.getHost();
        if (host == null || uri.getUserInfo() != null) return null;
        if (!scheme.equals("https") && !(allowCleartext && scheme.equals("http"))) return null;
        int port = uri.getPort();
        return scheme + "://" + host.toLowerCase(Locale.ROOT) + (port == -1 ? "" : ":" + port);
    }

    private static String originOf(Uri uri) {
        return normalise(uri.toString(), true);
    }

    /** True when the page asking is the app's own bundled start screen. */
    private boolean fromStartScreen() {
        String shown = getBridge().getWebView().getUrl();
        if (shown == null) return false;
        Uri uri = Uri.parse(shown);
        return getBridge().getScheme().equals(uri.getScheme()) && getBridge().getHost().equals(uri.getHost()) && uri.getPort() == -1;
    }

    @PluginMethod
    public void current(PluginCall call) {
        JSObject result = new JSObject();
        result.put("origin", enrolled(getContext()));
        call.resolve(result);
    }

    /** Enrol with an address. Only the start screen may; the activity restarts so the bridge is rebuilt trusting exactly that origin. */
    @PluginMethod
    public void enrol(PluginCall call) {
        if (!fromStartScreen()) {
            call.reject("Only the start screen can change the server");
            return;
        }
        String origin = normalise(call.getString("url"), debuggable(getContext()));
        if (origin == null) {
            call.reject("That address cannot be used: it must be https");
            return;
        }
        boolean changed = !origin.equals(enrolled(getContext()));
        SharedPreferences.Editor editor = getContext().getSharedPreferences(FILE, Context.MODE_PRIVATE).edit();
        editor.putString(KEY, origin).commit();
        JSObject result = new JSObject();
        result.put("origin", origin);
        call.resolve(result);
        if (changed) getActivity().runOnUiThread(() -> getActivity().recreate());
    }

    @PluginMethod
    public void forget(PluginCall call) {
        if (!fromStartScreen()) {
            call.reject("Only the start screen can change the server");
            return;
        }
        getContext().getSharedPreferences(FILE, Context.MODE_PRIVATE).edit().remove(KEY).commit();
        call.resolve();
        getActivity().runOnUiThread(() -> getActivity().recreate());
    }

    /** Every top-level navigation passes here first: the start screen, the enrolled origin and the identity provider load; anything else goes to the system browser. */
    @Override
    public Boolean shouldOverrideLoad(Uri url) {
        String scheme = url.getScheme() == null ? "" : url.getScheme().toLowerCase(Locale.ROOT);
        if (scheme.equals("blob") || url.toString().equals("about:blank")) return null;
        boolean web = scheme.equals("https") || scheme.equals("http");
        if (web) {
            boolean start = getBridge().getScheme().equals(scheme) && getBridge().getHost().equals(url.getHost()) && url.getPort() == -1;
            String origin = originOf(url);
            String enrolled = enrolled(getContext());
            if (start || (enrolled != null && enrolled.equals(origin))) return null;  // Capacitor's own checks follow
            if (scheme.equals("https") && url.getHost() != null && IDENTITY_PROVIDER_HOSTS.contains(url.getHost().toLowerCase(Locale.ROOT))) return false;
            try {
                getContext().startActivity(new Intent(Intent.ACTION_VIEW, url).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            } catch (ActivityNotFoundException ignored) {
                // Nothing on the phone can show it; it is still not loaded here.
            }
        }
        return true;
    }
}
