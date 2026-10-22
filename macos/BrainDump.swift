// Brain Dump: a Mac app that runs the Brain Dump server privately on this Mac and shows it
// in a native window. The bundled Python server starts when the app opens and stops when
// it quits. Only the AI model lives outside the app, in the Ollama app.
//
// Touch ID: once you've unlocked with your startup password, the app can keep the data key
// in the Keychain behind Touch ID. Then one touch at launch unlocks the data and signs you
// in, and the PIN screen offers Touch ID too. The key only ever travels to the server over
// the server's stdin (see app/macbridge.py), never through the environment or a file.

import Cocoa
import LocalAuthentication
import Security
import WebKit

let appName = "Brain Dump"
let preferredPort: UInt16 = 47_800

// MARK: - Data key in the Keychain, behind Touch ID

enum KeyResult {
    case key(String)
    case cancelled            // you pressed Cancel or chose the password instead
    case failed(String)       // try again later (Touch ID locked out, say); still turned on
    case invalidated          // fingerprints changed or the item is gone: turn on again
}

final class TouchIDKey {
    // Preferred: a Keychain item only Touch ID can open, which stops working if fingerprints
    // change (biometryCurrentSet). That needs the data protection keychain, which needs a
    // properly signed app; an ad-hoc signed build gets errSecMissingEntitlement. Fallback:
    // an ordinary Keychain item, read only after a Touch ID check, with the fingerprint set
    // remembered so a change still turns Touch ID off.
    static let biometry = "biometry"
    static let gated = "gated"

    private let service = "com.stevej763.braindump.data-key"
    private let account = "Brain Dump"
    private let defaults = UserDefaults.standard
    private let modeKey = "touchIDMode"
    private let domainKey = "touchIDDomainState"
    private let declinedKey = "touchIDDeclined"

    var mode: String? { defaults.string(forKey: modeKey) }
    var isOn: Bool { mode != nil }
    var declined: Bool {
        get { defaults.bool(forKey: declinedKey) }
        set { defaults.set(newValue, forKey: declinedKey) }
    }
    private(set) var lastStatus: OSStatus = errSecSuccess

    static var available: Bool {
        var error: NSError?
        return LAContext().canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error)
    }

    private func query(protected: Bool) -> [String: Any] {
        var q: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                kSecAttrService as String: service,
                                kSecAttrAccount as String: account]
        if protected { q[kSecUseDataProtectionKeychain as String] = true }
        return q
    }

    /// Stores the key and returns which protection worked (biometry or gated), or nil.
    func save(_ key: String) -> String? {
        remove()
        let data = Data(key.utf8)
        var cfError: Unmanaged<CFError>?
        if let access = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly,
                                                        .biometryCurrentSet, &cfError) {
            var q = query(protected: true)
            q[kSecValueData as String] = data
            q[kSecAttrAccessControl as String] = access
            lastStatus = SecItemAdd(q as CFDictionary, nil)
            if lastStatus == errSecSuccess {
                defaults.set(Self.biometry, forKey: modeKey)
                return Self.biometry
            }
            let why = lastStatus == errSecMissingEntitlement ? "the app isn't signed with a Team ID" : "OSStatus \(lastStatus)"
            NSLog("Brain Dump: Touch ID-only Keychain item unavailable (\(why)); using a Touch ID check instead.")
        }
        let context = LAContext()
        var error: NSError?
        guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &error) else { return nil }
        var q = query(protected: false)
        q[kSecValueData as String] = data
        q[kSecAttrLabel as String] = "Brain Dump data key"
        lastStatus = SecItemAdd(q as CFDictionary, nil)
        guard lastStatus == errSecSuccess else { return nil }
        defaults.set(context.evaluatedPolicyDomainState, forKey: domainKey)
        defaults.set(Self.gated, forKey: modeKey)
        return Self.gated
    }

    func remove() {
        SecItemDelete(query(protected: true) as CFDictionary)
        SecItemDelete(query(protected: false) as CFDictionary)
        defaults.removeObject(forKey: modeKey)
        defaults.removeObject(forKey: domainKey)
    }

    /// Asks for Touch ID and returns the key. Calls back on the main queue.
    func read(reason: String, completion: @escaping (KeyResult) -> Void) {
        let finish = { (result: KeyResult) in DispatchQueue.main.async { completion(result) } }
        guard let mode else { return finish(.invalidated) }
        if mode == Self.biometry {
            DispatchQueue.global(qos: .userInitiated).async {
                let context = LAContext()
                context.localizedReason = reason
                var q = self.query(protected: true)
                q[kSecReturnData as String] = true
                q[kSecMatchLimit as String] = kSecMatchLimitOne
                q[kSecUseAuthenticationContext as String] = context
                finish(self.copy(q))
            }
            return
        }
        let context = LAContext()
        context.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: reason) { ok, error in
            guard ok else {
                switch (error as? LAError)?.code {
                case .userCancel?, .appCancel?, .systemCancel?, .userFallback?: return finish(.cancelled)
                case .biometryNotEnrolled?: return finish(.invalidated)
                default: return finish(.failed(error?.localizedDescription ?? "Touch ID didn't work."))
                }
            }
            if let saved = self.defaults.data(forKey: self.domainKey), saved != context.evaluatedPolicyDomainState {
                return finish(.invalidated)  // fingerprints added or removed since Touch ID was turned on
            }
            var q = self.query(protected: false)
            q[kSecReturnData as String] = true
            q[kSecMatchLimit as String] = kSecMatchLimitOne
            finish(self.copy(q))
        }
    }

    private func copy(_ q: [String: Any]) -> KeyResult {
        var out: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &out)
        switch status {
        case errSecSuccess:
            guard let data = out as? Data, let key = String(data: data, encoding: .utf8) else { return .invalidated }
            return .key(key)
        case errSecUserCanceled: return .cancelled
        case errSecItemNotFound: return .invalidated
        default: return .failed("Keychain error \(status)")
        }
    }
}

// MARK: - App

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var server: Process?
    private var port: UInt16 = preferredPort
    private var quitting = false
    private var wakeLocks = 0
    private var wakeActivity: NSObjectProtocol?
    private var pendingDownloads: [WKDownload: URL] = [:]

    // The private channel to the server (see app/macbridge.py)
    private var toServer: FileHandle?
    private var fromServer = Data()
    private var waitingForKey: ((String?) -> Void)?
    private var waitingForCode: ((String?) -> Void)?
    private var serverState: String?
    private var pageReady = false
    private var afterPageLoads: [() -> Void] = []
    private var touchIDBusy = false
    private var unlockingWithTouchID = false
    private let touchID = TouchIDKey()

    private let fm = FileManager.default
    private lazy var resources = Bundle.main.resourceURL!
    private lazy var dataDir = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent(appName, isDirectory: true)
    private lazy var logURL = fm.urls(for: .libraryDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("Logs/\(appName)/server.log")

    func applicationDidFinishLaunching(_ notification: Notification) {
        signal(SIGPIPE, SIG_IGN)  // writing to a server that has just quit mustn't kill the app
        buildMenu()
        buildWindow()
        showMessage("Starting up…", detail: "")
        startServer()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            window.makeKeyAndOrderFront(nil)
            offerTouchIDLogin()  // closing the window locked it
        }
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationWillTerminate(_ notification: Notification) {
        quitting = true
        guard let server, server.isRunning else { return }
        server.terminate()  // SIGTERM lets uvicorn finish the request in flight
        let deadline = Date().addingTimeInterval(5)
        while server.isRunning && Date() < deadline { usleep(100_000) }
        if server.isRunning { kill(server.processIdentifier, SIGKILL) }
    }

    // MARK: - Server

    private func startServer() {
        do {
            try fm.createDirectory(at: dataDir, withIntermediateDirectories: true)
            try fm.createDirectory(at: logURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        } catch {
            return fail("Couldn't create the data folder: \(error.localizedDescription)")
        }
        port = portIsFree(preferredPort) ? preferredPort : (freePort() ?? preferredPort)

        let process = Process()
        process.executableURL = resources.appendingPathComponent("python/bin/python3")
        process.arguments = ["-I", resources.appendingPathComponent("server/launcher.py").path, String(port)]
        process.currentDirectoryURL = resources.appendingPathComponent("server")
        var env = ProcessInfo.processInfo.environment.filter { !$0.key.hasPrefix("PYTHON") }
        env["DATA_DIR"] = dataDir.path
        env["HF_HOME"] = dataDir.appendingPathComponent("hf-cache").path
        env["PYTHONDONTWRITEBYTECODE"] = "1"  // keep the signed app bundle unmodified
        env["PYTHONUNBUFFERED"] = "1"
        env["OLLAMA_URL"] = env["OLLAMA_URL"] ?? "http://localhost:11434"
        process.environment = env

        if !fm.fileExists(atPath: logURL.path) { fm.createFile(atPath: logURL.path, contents: nil) }
        if let log = try? FileHandle(forWritingTo: logURL) {
            log.truncateFile(atOffset: 0)
            process.standardError = log  // the server sends everything it prints here
        }
        let input = Pipe(), output = Pipe()
        process.standardInput = input
        process.standardOutput = output
        toServer = input.fileHandleForWriting
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            DispatchQueue.main.async { self?.received(data) }
        }
        process.terminationHandler = { [weak self] p in
            DispatchQueue.main.async {
                guard let self, !self.quitting else { return }
                self.fail("The Brain Dump server stopped unexpectedly (exit code \(p.terminationStatus)).")
            }
        }
        do {
            try process.run()
        } catch {
            return fail("Couldn't start the Brain Dump server: \(error.localizedDescription)")
        }
        server = process
        waitUntilReady(started: Date())
    }

    private func waitUntilReady(started: Date) {
        let url = URL(string: "http://127.0.0.1:\(port)/healthz")!
        URLSession.shared.dataTask(with: url) { [weak self] _, response, _ in
            DispatchQueue.main.async {
                guard let self, self.server?.isRunning == true else { return }
                if (response as? HTTPURLResponse)?.statusCode == 200 {
                    self.pageReady = false
                    self.webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(self.port)/")!))
                } else if Date().timeIntervalSince(started) > 60 {
                    self.fail("The Brain Dump server didn't start within a minute.")
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitUntilReady(started: started) }
                }
            }
        }.resume()
    }

    // MARK: - Talking to the server

    private func send(_ message: [String: String]) {
        guard let toServer, var data = try? JSONSerialization.data(withJSONObject: message) else { return }
        data.append(0x0A)
        try? toServer.write(contentsOf: data)
    }

    private func received(_ data: Data) {
        fromServer.append(data)
        while let newline = fromServer.firstIndex(of: 0x0A) {
            let line = fromServer[fromServer.startIndex..<newline]
            fromServer.removeSubrange(fromServer.startIndex...newline)
            guard let message = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let type = message["type"] as? String else { continue }
            handle(type, message)
        }
    }

    private func handle(_ type: String, _ message: [String: Any]) {
        switch type {
        case "state":
            serverState = message["state"] as? String
            // Locked and Touch ID is on: one touch unlocks the data and signs you in.
            if serverState == "locked" && touchID.isOn { unlockWithTouchID() }
        case "unlocked":
            serverState = "unlocked"
            let viaTouchID = unlockingWithTouchID
            unlockingWithTouchID = false
            if viaTouchID {
                signInWithCode()
            } else if message["password"] as? Bool == true && !touchID.isOn && !touchID.declined
                        && TouchIDKey.available {
                whenPageReady { self.offerTouchID() }
            }
        case "key":
            waitingForKey?(message["key"] as? String)
            waitingForKey = nil
        case "login_code":
            waitingForCode?(message["code"] as? String)
            waitingForCode = nil
        case "error":
            switch message["for"] as? String {
            case "unlock":
                // The saved key no longer fits (the data was reset, say): forget it.
                unlockingWithTouchID = false
                touchID.remove()
                updatePageTouchID()
                whenPageReady { self.webView.evaluateJavaScript("window.braindumpRefresh && window.braindumpRefresh()") }
            case "get_key":
                waitingForKey?(nil)
                waitingForKey = nil
            default:
                waitingForCode?(nil)
                waitingForCode = nil
            }
        default:
            break
        }
    }

    private func whenPageReady(_ work: @escaping () -> Void) {
        if pageReady { work() } else { afterPageLoads.append(work) }
    }

    // MARK: - Touch ID

    private func unlockWithTouchID() {
        guard !touchIDBusy else { return }
        touchIDBusy = true
        touchID.read(reason: "unlock Brain Dump") { [weak self] result in
            guard let self else { return }
            self.touchIDBusy = false
            switch result {
            case .key(let key):
                self.unlockingWithTouchID = true
                self.send(["type": "unlock", "key": key])
            case .invalidated:
                self.touchIDTurnedOff()
            case .failed(let why):
                NSLog("Brain Dump: Touch ID unlock failed: \(why)")
            case .cancelled:
                break  // type the password instead
            }
        }
    }

    /// Touch ID in place of the PIN.
    private func signInWithTouchID() {
        guard !touchIDBusy, touchID.isOn, serverState == "unlocked" else { return }
        touchIDBusy = true
        // Reading the key proves it's you and that Touch ID is still set up as it was.
        touchID.read(reason: "sign in to Brain Dump") { [weak self] result in
            guard let self else { return }
            self.touchIDBusy = false
            switch result {
            case .key: self.signInWithCode()
            case .invalidated: self.touchIDTurnedOff()
            case .failed(let why): NSLog("Brain Dump: Touch ID sign-in failed: \(why)")
            case .cancelled: break
            }
        }
    }

    private func signInWithCode() {
        waitingForCode = { [weak self] code in
            guard let self else { return }
            self.whenPageReady {
                if let code {
                    self.webView.callAsyncJavaScript("return await window.braindumpMacLogin(code)",
                                                     arguments: ["code": code], in: nil, in: .page)
                } else {
                    self.webView.evaluateJavaScript("window.braindumpRefresh && window.braindumpRefresh()")
                }
            }
        }
        send(["type": "login_code"])
    }

    private func offerTouchIDLogin() {
        guard touchID.isOn, serverState == "unlocked" else { return }
        webView.evaluateJavaScript("!document.querySelector('#login').hidden") { [weak self] shown, _ in
            if shown as? Bool == true { self?.signInWithTouchID() }
        }
    }

    private func touchIDTurnedOff() {
        touchID.remove()
        updatePageTouchID()
        let alert = NSAlert()
        alert.messageText = "Touch ID was turned off"
        alert.informativeText = "Your fingerprints changed, so Brain Dump needs your startup password once more. " +
            "Afterwards it will offer to turn Touch ID back on."
        whenPageReady { alert.beginSheetModal(for: self.window) { _ in
            self.webView.evaluateJavaScript("window.braindumpRefresh && window.braindumpRefresh()")
        } }
    }

    private func offerTouchID() {
        let alert = NSAlert()
        alert.messageText = "Use Touch ID for Brain Dump?"
        alert.informativeText = "Unlock Brain Dump and sign in with your fingerprint instead of typing your " +
            "password and PIN. Your startup password still works, and you can turn this off in the Brain Dump menu."
        alert.addButton(withTitle: "Use Touch ID")
        alert.addButton(withTitle: "Not Now")
        alert.beginSheetModal(for: window) { response in
            if response == .alertFirstButtonReturn { self.turnOnTouchID() } else { self.touchID.declined = true }
        }
    }

    private func turnOnTouchID() {
        waitingForKey = { [weak self] key in
            guard let self else { return }
            guard let key, let mode = self.touchID.save(key) else {
                let alert = NSAlert()
                alert.messageText = "Couldn't turn on Touch ID"
                alert.informativeText = key == nil
                    ? "Unlock Brain Dump with your startup password first."
                    : "The Keychain refused to save the key (error \(self.touchID.lastStatus))."
                alert.beginSheetModal(for: self.window)
                return
            }
            NSLog("Brain Dump: Touch ID turned on (\(mode == TouchIDKey.biometry ? "Touch ID-only Keychain item" : "Touch ID check and Keychain item"))")
            self.touchID.declined = false
            self.updatePageTouchID()
        }
        send(["type": "get_key"])
    }

    @objc func toggleTouchID(_ sender: Any?) {
        if touchID.isOn {
            touchID.remove()
            touchID.declined = true
            updatePageTouchID()
        } else if !TouchIDKey.available {
            let alert = NSAlert()
            alert.messageText = "Touch ID isn't available"
            alert.informativeText = "Set up Touch ID in System Settings first."
            alert.beginSheetModal(for: window)
        } else {
            turnOnTouchID()
        }
    }

    /// Tells the page whether to show "Use Touch ID" buttons.
    private func touchIDScript() -> String { "window.braindumpMac = { touchID: \(touchID.isOn) };" }

    private func updatePageTouchID() {
        installScripts()
        webView.evaluateJavaScript(touchIDScript() + " document.querySelector('#touch-id-login').hidden = !\(touchID.isOn);")
    }

    private func fail(_ message: String) {
        showMessage(message, detail: "Details are in \(logURL.path). Quit and reopen Brain Dump to try again.")
    }

    private func portIsFree(_ port: UInt16) -> Bool { bindLoopback(port) != nil }

    private func freePort() -> UInt16? { bindLoopback(0) }

    /// Binds a loopback socket to `port` (0 = any) and returns the bound port, or nil if taken.
    private func bindLoopback(_ port: UInt16) -> UInt16? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { Darwin.close(fd) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = port.bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafeMutablePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(fd, $0, len) == 0 && getsockname(fd, $0, &len) == 0
            }
        }
        return bound ? UInt16(bigEndian: addr.sin_port) : nil
    }

    // MARK: - Window

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.mediaTypesRequiringUserActionForPlayback = []

        // WKWebView has no Screen Wake Lock API, which the app uses to stop the Mac sleeping
        // mid-recording. Provide one backed by a native activity.
        let wakeLock = """
        if (!navigator.wakeLock) {
          Object.defineProperty(navigator, "wakeLock", { value: { request: async () => {
            const post = (on) => window.webkit.messageHandlers.wakeLock.postMessage(on);
            post(true);
            let released = false;
            return { type: "screen", get released() { return released; },
                     addEventListener() {}, removeEventListener() {},
                     release: async () => { if (!released) { released = true; post(false); } } };
          } } });
        }
        """
        wakeLockScript = wakeLock
        config.userContentController.add(self, name: "wakeLock")
        config.userContentController.add(self, name: "braindump")

        webView = WKWebView(frame: .zero, configuration: config)
        installScripts()
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.allowsBackForwardNavigationGestures = false

        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = appName
        window.minSize = NSSize(width: 360, height: 480)
        window.contentView = webView
        window.setFrameAutosaveName("Main")
        if window.frame.origin == .zero { window.center() }
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.makeKeyAndOrderFront(nil)
    }

    private var wakeLockScript = ""

    private func installScripts() {
        let controller = webView.configuration.userContentController
        controller.removeAllUserScripts()
        for source in [wakeLockScript, touchIDScript()] {
            controller.addUserScript(WKUserScript(source: source, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        }
    }

    private func showMessage(_ title: String, detail: String) {
        func esc(_ s: String) -> String {
            s.replacingOccurrences(of: "&", with: "&amp;").replacingOccurrences(of: "<", with: "&lt;")
        }
        let html = """
        <!doctype html><meta charset="utf-8"><style>
        :root { color-scheme: light dark; }
        body { font: 15px -apple-system, sans-serif; display: flex; flex-direction: column; align-items: center;
               justify-content: center; height: 100vh; margin: 0 24px; text-align: center; color: #555; }
        h1 { font-size: 17px; font-weight: 600; color: CanvasText; } p { max-width: 420px; line-height: 1.4; }
        </style><h1>\(esc(title))</h1><p>\(esc(detail))</p>
        """
        webView.loadHTMLString(html, baseURL: nil)
    }

    @objc func reloadPage(_ sender: Any?) {
        if server?.isRunning == true { webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(port)/")!)) }
    }

    @objc func openDataFolder(_ sender: Any?) { NSWorkspace.shared.open(dataDir) }

    @objc func openLog(_ sender: Any?) { NSWorkspace.shared.activateFileViewerSelecting([logURL]) }

    private func buildMenu() {
        let main = NSMenu()

        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About \(appName)",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Use Touch ID", action: #selector(toggleTouchID(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide \(appName)", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        let hideOthers = appMenu.addItem(withTitle: "Hide Others",
                                         action: #selector(NSApplication.hideOtherApplications(_:)), keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(withTitle: "Show All", action: #selector(NSApplication.unhideAllApplications(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit \(appName)", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")

        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")

        let viewMenu = NSMenu(title: "View")
        viewMenu.addItem(withTitle: "Reload", action: #selector(reloadPage(_:)), keyEquivalent: "r")
        viewMenu.addItem(.separator())
        viewMenu.addItem(withTitle: "Show Data Folder", action: #selector(openDataFolder(_:)), keyEquivalent: "")
        viewMenu.addItem(withTitle: "Show Server Log", action: #selector(openLog(_:)), keyEquivalent: "")

        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        windowMenu.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        NSApp.windowsMenu = windowMenu

        for (title, menu) in [(appName, appMenu), ("Edit", editMenu), ("View", viewMenu), ("Window", windowMenu)] {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            item.submenu = menu
            main.addItem(item)
        }
        NSApp.mainMenu = main
    }

    private func isLocal(_ url: URL?) -> Bool {
        guard let url else { return false }
        return url.scheme == "about" || (url.host == "127.0.0.1" && url.port == Int(port))
    }
}

// MARK: - Window behaviour

extension AppDelegate: NSMenuItemValidation {
    func validateMenuItem(_ item: NSMenuItem) -> Bool {
        if item.action == #selector(toggleTouchID(_:)) {
            item.state = touchID.isOn ? .on : .off
            return touchID.isOn || serverState == "unlocked"
        }
        return true
    }
}

extension AppDelegate: NSWindowDelegate {
    // Closing the window keeps the app (and any recording that's still saving) running, but
    // locks it, so reopening it asks for the PIN.
    func windowWillClose(_ notification: Notification) {
        webView.evaluateJavaScript("window.braindumpLock && window.braindumpLock()", completionHandler: nil)
    }
}

// MARK: - Web view behaviour

extension AppDelegate: WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate, WKScriptMessageHandler {
    // Links to other sites open in the default browser.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let url = action.request.url, !isLocal(url), action.navigationType == .linkActivated {
            NSWorkspace.shared.open(url)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = action.request.url {
            if isLocal(url) { webView.load(action.request) } else { NSWorkspace.shared.open(url) }
        }
        return nil
    }

    // Report and CSV exports are sent as attachments: save them with a save panel.
    func webView(_ webView: WKWebView, decidePolicyFor response: WKNavigationResponse,
                 decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        let disposition = (response.response as? HTTPURLResponse)?.value(forHTTPHeaderField: "Content-Disposition") ?? ""
        decisionHandler(disposition.lowercased().hasPrefix("attachment") || !response.canShowMIMEType ? .download : .allow)
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func download(_ download: WKDownload, decideDestinationUsing response: URLResponse,
                  suggestedFilename: String, completionHandler: @escaping (URL?) -> Void) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = suggestedFilename
        panel.directoryURL = fm.urls(for: .downloadsDirectory, in: .userDomainMask).first
        panel.beginSheetModal(for: window) { result in
            guard result == .OK, let url = panel.url else { return completionHandler(nil) }
            try? self.fm.removeItem(at: url)  // the panel already confirmed replacing it
            self.pendingDownloads[download] = url
            completionHandler(url)
        }
    }

    func downloadDidFinish(_ download: WKDownload) {
        if let url = pendingDownloads.removeValue(forKey: download) {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        }
    }

    func download(_ download: WKDownload, didFailWithError error: Error, resumeData: Data?) {
        pendingDownloads.removeValue(forKey: download)
        NSAlert(error: error).beginSheetModal(for: window)
    }

    // Microphone: macOS asks once for the app; the page itself is always ours.
    func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        decisionHandler(origin.host == "127.0.0.1" && type == .microphone ? .grant : .deny)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { completionHandler($0 == .alertFirstButtonReturn) }
    }

    func webView(_ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.beginSheetModal(for: window) { completionHandler($0 == .OK ? panel.urls : nil) }
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        setWakeLocks(0)  // a reload drops the page's locks without releasing them
        pageReady = false
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        guard isLocal(webView.url), webView.url?.scheme != "about" else { return }
        pageReady = true
        let work = afterPageLoads
        afterPageLoads = []
        work.forEach { $0() }
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        if message.name == "braindump" {
            guard isLocal(message.frameInfo.request.url),
                  let action = (message.body as? [String: Any])?["action"] as? String else { return }
            if action == "unlock" && serverState == "locked" { unlockWithTouchID() }
            if action == "login" { signInWithTouchID() }
            return
        }
        guard message.name == "wakeLock", let on = message.body as? Bool else { return }
        setWakeLocks(wakeLocks + (on ? 1 : -1))
    }

    private func setWakeLocks(_ count: Int) {
        wakeLocks = max(0, count)
        if wakeLocks > 0, wakeActivity == nil {
            wakeActivity = ProcessInfo.processInfo.beginActivity(
                options: [.idleDisplaySleepDisabled, .userInitiated], reason: "Recording")
        } else if wakeLocks == 0, let activity = wakeActivity {
            ProcessInfo.processInfo.endActivity(activity)
            wakeActivity = nil
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.activate(ignoringOtherApps: true)
app.run()
