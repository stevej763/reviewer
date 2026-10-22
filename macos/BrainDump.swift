// Brain Dump: a Mac app that runs the Brain Dump server privately on this Mac and shows it
// in a native window. The bundled Python server starts when the app opens and stops when
// it quits. Only the AI model lives outside the app, in the Ollama app.

import Cocoa
import WebKit

let appName = "Brain Dump"
let preferredPort: UInt16 = 47_800

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var server: Process?
    private var port: UInt16 = preferredPort
    private var quitting = false
    private var wakeLocks = 0
    private var wakeActivity: NSObjectProtocol?
    private var pendingDownloads: [WKDownload: URL] = [:]

    private let fm = FileManager.default
    private lazy var resources = Bundle.main.resourceURL!
    private lazy var dataDir = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent(appName, isDirectory: true)
    private lazy var logURL = fm.urls(for: .libraryDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("Logs/\(appName)/server.log")

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        showMessage("Starting up…", detail: "")
        startServer()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if !flag { window.makeKeyAndOrderFront(nil) }
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
            process.standardOutput = log
            process.standardError = log
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
                    self.webView.load(URLRequest(url: URL(string: "http://127.0.0.1:\(self.port)/")!))
                } else if Date().timeIntervalSince(started) > 60 {
                    self.fail("The Brain Dump server didn't start within a minute.")
                } else {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.waitUntilReady(started: started) }
                }
            }
        }.resume()
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
        config.userContentController.addUserScript(
            WKUserScript(source: wakeLock, injectionTime: .atDocumentStart, forMainFrameOnly: true))
        config.userContentController.add(self, name: "wakeLock")

        webView = WKWebView(frame: .zero, configuration: config)
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
        window.makeKeyAndOrderFront(nil)
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
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
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
