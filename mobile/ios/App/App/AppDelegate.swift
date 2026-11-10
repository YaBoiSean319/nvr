import UIKit
import Capacitor

@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?
    private var cover: UIView?

    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // Override point for customization after application launch.
        return true
    }

    // The push plugin learns the phone's token (or why there is none) through these.
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        NotificationCenter.default.post(name: .capacitorDidRegisterForRemoteNotifications, object: deviceToken)
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        NotificationCenter.default.post(name: .capacitorDidFailToRegisterForRemoteNotifications, object: error)
    }

    func applicationWillResignActive(_ application: UIApplication) {
        // Camera pictures stay out of the app switcher's snapshot.
        guard cover == nil, let window = UIApplication.shared.connectedScenes.compactMap({ ($0 as? UIWindowScene)?.windows.first }).first else { return }
        let blur = UIVisualEffectView(effect: UIBlurEffect(style: .systemThickMaterial))
        blur.frame = window.bounds
        blur.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        window.addSubview(blur)
        cover = blur

        // Sent when the application is about to move from active to inactive state. This can occur for certain types of temporary interruptions (such as an incoming phone call or SMS message) or when the user quits the application and it begins the transition to the background state.
        // Use this method to pause ongoing tasks, disable timers, and invalidate graphics rendering callbacks. Games should use this method to pause the game.
    }

    func applicationDidEnterBackground(_ application: UIApplication) {
        // Use this method to release shared resources, save user data, invalidate timers, and store enough application state information to restore your application to its current state in case it is terminated later.
        // If your application supports background execution, this method is called instead of applicationWillTerminate: when the user quits.
    }

    func applicationWillEnterForeground(_ application: UIApplication) {
        // Called as part of the transition from the background to the active state; here you can undo many of the changes made on entering the background.
    }

    func applicationDidBecomeActive(_ application: UIApplication) {
        cover?.removeFromSuperview()
        cover = nil
        // Restart any tasks that were paused (or not yet started) while the application was inactive. If the application was previously in the background, optionally refresh the user interface.
    }

    func applicationWillTerminate(_ application: UIApplication) {
        // Called when the application is about to terminate. Save data if appropriate. See also applicationDidEnterBackground:.
    }

    func application(_ application: UIApplication,
                     configurationForConnecting connectingSceneSession: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: "Default Configuration",
                                          sessionRole: connectingSceneSession.role)
        config.delegateClass = SceneDelegate.self
        return config
    }
}

// MARK: - The enrolled server (#299)
// Kept in this file so the Xcode project needs no new entries: the plugin below and the view controller that registers it.

import WebKit

/// The one server this phone is enrolled with. The address lives in storage only native code writes, and only the bundled start screen may change it.
/// Every top-level navigation passes through `shouldOverrideLoad`: the start screen, the enrolled origin and the identity provider's sign-in pages load,
/// and anything else goes to the system browser instead of the view that can reach the native plugins and the Keychain.
@objc(EnrolledServerPlugin)
public class EnrolledServerPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "EnrolledServerPlugin"
    public let jsName = "EnrolledServer"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "current", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "enrol", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "forget", returnType: CAPPluginReturnPromise)
    ]
    private static let key = "nvr.enrolled.origin"
    private static let identityProviderHosts: Set<String> = ["login.microsoftonline.com", "login.live.com"]

    static var enrolled: String? { UserDefaults.standard.string(forKey: key) }

    /// scheme://host[:port], for https; plain http only for a name on the local network (the app's transport security allows nothing else).
    static func origin(of url: URL, allowCleartext: Bool) -> String? {
        guard let scheme = url.scheme?.lowercased(), let host = url.host?.lowercased(), url.user == nil, url.password == nil else { return nil }
        let local = !host.contains(".") || host.hasSuffix(".local") || host.range(of: #"^(10\.|127\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)"#, options: .regularExpression) != nil
        guard scheme == "https" || (allowCleartext && scheme == "http" && local) else { return nil }
        return "\(scheme)://\(host)" + (url.port.map { ":\($0)" } ?? "")
    }

    private func fromStartScreen() -> Bool {
        guard let shown = bridge?.webView?.url, let local = bridge?.config.localURL else { return false }
        return shown.scheme == local.scheme && shown.host == local.host && shown.port == local.port
    }

    @objc func current(_ call: CAPPluginCall) {
        call.resolve(["origin": EnrolledServerPlugin.enrolled as Any])
    }

    @objc func enrol(_ call: CAPPluginCall) {
        guard fromStartScreen() else { call.reject("Only the start screen can change the server"); return }
        guard let address = call.getString("url"), address.count <= 300, let url = URL(string: address.trimmingCharacters(in: .whitespaces)),
              let origin = EnrolledServerPlugin.origin(of: url, allowCleartext: true) else {
            call.reject("That address cannot be used: it must be https, or http on your own network")
            return
        }
        UserDefaults.standard.set(origin, forKey: EnrolledServerPlugin.key)
        call.resolve(["origin": origin])
    }

    @objc func forget(_ call: CAPPluginCall) {
        guard fromStartScreen() else { call.reject("Only the start screen can change the server"); return }
        UserDefaults.standard.removeObject(forKey: EnrolledServerPlugin.key)
        call.resolve()
    }

    @objc public override func shouldOverrideLoad(_ navigationAction: WKNavigationAction) -> NSNumber? {
        guard let url = navigationAction.request.url, let scheme = url.scheme?.lowercased() else { return NSNumber(value: true) }
        if scheme == "blob" || url.absoluteString == "about:blank" { return nil }
        guard scheme == "https" || scheme == "http" || scheme == "capacitor" else { return NSNumber(value: true) }
        if let local = bridge?.config.localURL, url.scheme == local.scheme, url.host == local.host, url.port == local.port { return nil }
        if let enrolled = EnrolledServerPlugin.enrolled, EnrolledServerPlugin.origin(of: url, allowCleartext: true) == enrolled { return nil }
        if scheme == "https", let host = url.host?.lowercased(), EnrolledServerPlugin.identityProviderHosts.contains(host) { return NSNumber(value: false) }
        if scheme != "capacitor", navigationAction.targetFrame?.isMainFrame ?? true { UIApplication.shared.open(url, options: [:], completionHandler: nil) }
        return NSNumber(value: true)
    }
}

/// Capacitor's view controller with the enrolled-server plugin registered. The main storyboard names this class.
class NvrBridgeViewController: CAPBridgeViewController {
    override func capacitorDidLoad() {
        bridge?.registerPluginInstance(EnrolledServerPlugin())
    }
}
