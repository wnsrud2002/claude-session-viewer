import Cocoa
import WebKit

// 화면·서버는 웹 코드 그대로 두고, sessions.py 를 자식 프로세스로 띄워 WebKit 창에 보여주는 껍데기
final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?
    // 8765 를 이미 쓰는 다른(오래된) 서버에 붙지 않도록 앱마다 빈 포트를 새로 받음
    let port = freePort()
    var url: URL { URL(string: "http://127.0.0.1:\(port)")! }

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        web = WKWebView(frame: .zero)
        web.uiDelegate = self
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
        window.title = "Session Viewer"
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("SessionViewer")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startServer()
        waitForServer(tries: 50) { self.web.load(URLRequest(url: self.url)) }
    }

    func startServer() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = [Bundle.main.path(forResource: "sessions", ofType: "py")!]
        // SV_WATCH_PARENT: 앱이 강제 종료돼도 서버가 고아로 남지 않게 서버 스스로 부모를 지켜봄
        p.environment = ProcessInfo.processInfo.environment.merging(
            ["SV_PORT": String(port), "SV_WATCH_PARENT": "1"]) { $1 }
        do {
            try p.run()
        } catch {
            fail("서버를 실행하지 못했습니다", error.localizedDescription)
        }
        server = p
    }

    func fail(_ title: String, _ detail: String) {
        let a = NSAlert()
        a.messageText = title
        a.informativeText = detail
        a.runModal()
        NSApp.terminate(nil)
    }

    func waitForServer(tries: Int, then done: @escaping () -> Void) {
        URLSession.shared.dataTask(with: url.appendingPathComponent("api/sessions")) { _, resp, _ in
            DispatchQueue.main.async {
                if (resp as? HTTPURLResponse)?.statusCode == 200 {
                    done()
                } else if let s = self.server, !s.isRunning {
                    self.fail("서버가 바로 종료됐습니다", "종료 코드 \(s.terminationStatus)")
                } else if tries > 0 {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { self.waitForServer(tries: tries - 1, then: done) }
                } else {
                    self.fail("서버를 시작하지 못했습니다", "/usr/bin/python3 가 있는지 확인하세요.")
                }
            }
        }.resume()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ note: Notification) { server?.terminate() }

    // WKWebView 는 UI delegate 가 없으면 confirm() 을 항상 false 로 돌려 삭제 확인이 동작하지 않음
    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "확인")
        a.addButton(withTitle: "취소")
        completionHandler(a.runModal() == .alertFirstButtonReturn)
    }

    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = message
        a.runModal()
        completionHandler()
    }

    @objc func reload() { web.reload() }

    func buildMenu() {
        let main = NSMenu()
        func submenu(_ title: String, _ items: [(String, Selector, String)]) {
            let item = NSMenuItem()
            let menu = NSMenu(title: title)
            items.forEach { menu.addItem(withTitle: $0.0, action: $0.1, keyEquivalent: $0.2) }
            item.submenu = menu
            main.addItem(item)
        }
        submenu("Session Viewer", [("Session Viewer 종료", #selector(NSApplication.terminate(_:)), "q")])
        // 메뉴에 등록된 단축키만 WebView 로 전달되므로 ⌘C·⌘V 등을 쓰려면 편집 메뉴가 필요함
        submenu("편집", [("실행 취소", Selector(("undo:")), "z"), ("잘라내기", #selector(NSText.cut(_:)), "x"),
                         ("복사", #selector(NSText.copy(_:)), "c"), ("붙여넣기", #selector(NSText.paste(_:)), "v"),
                         ("전체 선택", #selector(NSText.selectAll(_:)), "a")])
        submenu("보기", [("새로고침", #selector(reload), "r")])
        NSApp.mainMenu = main
    }
}

func freePort() -> Int {
    // ponytail: 포트를 확인하고 닫은 뒤 서버가 여는 사이에 다른 프로그램이 가로챌 수 있음. 그러면 "서버가 바로 종료됐습니다" 경고로 드러남
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    defer { close(fd) }
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    var len = socklen_t(MemoryLayout<sockaddr_in>.size)
    withUnsafeMutablePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            _ = bind(fd, $0, len)
            _ = getsockname(fd, $0, &len)
        }
    }
    return Int(UInt16(bigEndian: addr.sin_port))
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
