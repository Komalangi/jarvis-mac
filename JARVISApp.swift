// JARVIS — нативная оболочка: полноэкранный дашборд, виджет на рабочем столе, Марк-0 поверх окон,
// голос (распознавание + нейросетевой синтез через ядро), иконка в меню-баре, горячая клавиша ⌥⌘J.
// Собирается автоматически из jarvis.py (swiftc из Command Line Tools).
import Cocoa
import WebKit
import ApplicationServices
import Speech
import AVFoundation
import Carbon

let PYTHON = "__PYTHON__"
let SCRIPT = (Bundle.main.resourcePath ?? "") + "/jarvis.py"
let LOG = NSHomeDirectory() + "/Library/Logs/JARVIS.log"
let BASE = "http://127.0.0.1:7777"


// ───────── утилиты ─────────

func jarvisToken() -> String? {
    let path = NSHomeDirectory() + "/Library/Application Support/JARVIS/token"
    return (try? String(contentsOfFile: path, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
}

func appendLog(_ text: String) {
    let line = "[\(Date())] \(text)\n"
    guard let data = line.data(using: .utf8) else { return }
    if let h = FileHandle(forWritingAtPath: LOG) {
        h.seekToEndOfFile(); h.write(data); h.closeFile()
    } else {
        FileManager.default.createFile(atPath: LOG, contents: data)
    }
}

/// Строка → безопасный литерал для JavaScript.
func jsStr(_ s: String) -> String {
    if let d = try? JSONSerialization.data(withJSONObject: [s]), let a = String(data: d, encoding: .utf8) {
        return String(a.dropFirst().dropLast())
    }
    return "''"
}

func jsJSON(_ obj: Any) -> String {
    if let d = try? JSONSerialization.data(withJSONObject: obj), let s = String(data: d, encoding: .utf8) { return s }
    return "{}"
}

func writeResult(_ text: String) {
    let dir = NSHomeDirectory() + "/Library/Application Support/JARVIS"
    try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
    try? text.write(toFile: dir + "/click_result", atomically: true, encoding: .utf8)
}

func runOpen(_ args: [String]) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    p.arguments = args
    try? p.run()
    p.waitUntilExit()
}

func waitFor(_ seconds: Double, _ check: () -> Bool) -> Bool {
    let end = Date().addingTimeInterval(seconds)
    while Date() < end {
        if check() { return true }
        usleep(300_000)
    }
    return check()
}

func corePost(_ path: String, _ body: [String: Any], timeout: TimeInterval = 25, _ done: (([String: Any]) -> Void)? = nil) {
    var req = URLRequest(url: URL(string: BASE + path)!, timeoutInterval: timeout)
    req.httpMethod = "POST"
    req.setValue("application/json", forHTTPHeaderField: "Content-Type")
    if let tok = jarvisToken() { req.setValue(tok, forHTTPHeaderField: "X-Jarvis-Token") }
    req.httpBody = try? JSONSerialization.data(withJSONObject: body)
    URLSession.shared.dataTask(with: req) { data, _, _ in
        let obj = (data.flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]) ?? [:]
        DispatchQueue.main.async { done?(obj) }
    }.resume()
}

func coreGet(_ path: String, _ done: @escaping ([String: Any]) -> Void) {
    guard let url = URL(string: BASE + path) else { return }
    var req = URLRequest(url: url, timeoutInterval: 20)
    req.cachePolicy = .reloadIgnoringLocalCacheData
    URLSession.shared.dataTask(with: req) { data, _, _ in
        if let d = data, let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
            DispatchQueue.main.async { done(obj) }
        }
    }.resume()
}

/// Медиаклавиши: 16 — пуск/пауза, 17 — следующий трек, 18 — предыдущий.
func mediaKey(_ key: Int32) {
    for down in [true, false] {
        let flags = NSEvent.ModifierFlags(rawValue: down ? 0xa00 : 0xb00)
        let data1 = Int((key << 16) | ((down ? 0xa : 0xb) << 8))
        let ev = NSEvent.otherEvent(with: .systemDefined, location: .zero, modifierFlags: flags, timestamp: 0,
                                    windowNumber: 0, context: nil, subtype: 8, data1: data1, data2: -1)
        ev?.cgEvent?.post(tap: .cghidEventTap)
        usleep(30_000)
    }
}

func userIdleSeconds() -> Double {
    let any = CGEventType(rawValue: ~0)!
    return CGEventSource.secondsSinceLastEventType(.combinedSessionState, eventType: any)
}


// ───────── режим «подключить Happ» (запускается как: JARVIS --connect-happ) ─────────

func happApp() -> NSRunningApplication? {
    for id in ["com.happ", "su.ffg.happ"] {
        if let a = NSRunningApplication.runningApplications(withBundleIdentifier: id).first { return a }
    }
    return NSWorkspace.shared.runningApplications.first { $0.localizedName == "Happ" }
}

func happWindow(pid: pid_t) -> CGRect? {
    guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return nil }
    var best: CGRect? = nil
    for w in list {
        guard let owner = (w[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value, owner == pid,
              let layer = (w[kCGWindowLayer as String] as? NSNumber)?.intValue, layer == 0,
              let bd = w[kCGWindowBounds as String] as? NSDictionary,
              let r = CGRect(dictionaryRepresentation: bd as CFDictionary) else { continue }
        if r.width > 300 && r.height > 300 {
            if best == nil || r.width * r.height > best!.width * best!.height { best = r }
        }
    }
    return best
}

func click(at point: CGPoint) {
    let src = CGEventSource(stateID: .hidSystemState)
    let saved = CGEvent(source: nil)?.location
    CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(150_000)
    CGEvent(mouseEventSource: src, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(90_000)
    CGEvent(mouseEventSource: src, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    usleep(200_000)
    if let s = saved {
        CGEvent(mouseEventSource: src, mouseType: .mouseMoved, mouseCursorPosition: s, mouseButton: .left)?.post(tap: .cghidEventTap)
    }
}

// ───────── Claude: открыть нужную сессию и отправить заказ ─────────

func claudeApp() -> NSRunningApplication? {
    return NSWorkspace.shared.runningApplications.first {
        $0.localizedName == "Claude" || ($0.bundleIdentifier ?? "").hasPrefix("com.anthropic.claude")
    }
}

func axAttr(_ el: AXUIElement, _ name: String) -> AnyObject? {
    var v: AnyObject?
    return AXUIElementCopyAttributeValue(el, name as CFString, &v) == .success ? v : nil
}

func axNorm(_ s: String) -> String {
    let low = s.lowercased().replacingOccurrences(of: "ё", with: "е")
    let kept = low.unicodeScalars.filter { CharacterSet.alphanumerics.contains($0) || $0 == " " }
    return String(String.UnicodeScalarView(kept)).split(separator: " ").joined(separator: " ")
}

func axFind(_ root: AXUIElement, _ target: String) -> AXUIElement? {
    let want = axNorm(target)
    var queue: [AXUIElement] = [root]
    var i = 0
    while i < queue.count && i < 8000 {
        let el = queue[i]; i += 1
        let role = (axAttr(el, kAXRoleAttribute as String) as? String) ?? ""
        if role != "AXWindow" && role != "AXApplication" && role != "AXTextArea" && role != "AXTextField" {
            for a in [kAXTitleAttribute, kAXValueAttribute, kAXDescriptionAttribute] {
                if let t = axAttr(el, a as String) as? String {
                    let n = axNorm(t)
                    if n == want || n.hasPrefix(want + " ") { return el }
                }
            }
        }
        if let kids = axAttr(el, kAXChildrenAttribute as String) as? [AXUIElement] { queue.append(contentsOf: kids) }
    }
    return nil
}

func axCenter(_ el: AXUIElement) -> CGPoint? {
    guard let pv = axAttr(el, kAXPositionAttribute as String), let sv = axAttr(el, kAXSizeAttribute as String),
          CFGetTypeID(pv) == AXValueGetTypeID(), CFGetTypeID(sv) == AXValueGetTypeID() else { return nil }
    var p = CGPoint.zero, sz = CGSize.zero
    AXValueGetValue(pv as! AXValue, .cgPoint, &p)
    AXValueGetValue(sv as! AXValue, .cgSize, &sz)
    if sz.width < 2 || sz.height < 2 { return nil }
    return CGPoint(x: p.x + sz.width / 2, y: p.y + sz.height / 2)
}

func pressKey(_ code: CGKeyCode, cmd: Bool = false) {
    let src = CGEventSource(stateID: .hidSystemState)
    for down in [true, false] {
        let e = CGEvent(keyboardEventSource: src, virtualKey: code, keyDown: down)
        if cmd { e?.flags = .maskCommand }
        e?.post(tap: .cghidEventTap)
        usleep(60_000)
    }
}

func sendToClaude(session: String) -> String? {
    if !AXIsProcessTrusted() {
        return "Нет разрешения «Универсальный доступ» для JARVIS. Заказ лежит в буфере, вставь его сам."
    }
    if claudeApp() == nil { runOpen(["-a", "Claude"]) }
    guard waitFor(15, { claudeApp() != nil }), let app = claudeApp() else {
        return "Не смог открыть Claude. Заказ лежит в буфере."
    }
    app.activate(options: [.activateIgnoringOtherApps])
    _ = waitFor(6) { NSWorkspace.shared.frontmostApplication?.processIdentifier == app.processIdentifier }
    let axApp = AXUIElementCreateApplication(app.processIdentifier)
    AXUIElementSetAttributeValue(axApp, "AXManualAccessibility" as CFString, kCFBooleanTrue)
    usleep(700_000)
    var found: AXUIElement? = nil
    for _ in 0..<4 {
        let root = (axAttr(axApp, kAXFocusedWindowAttribute as String)).map { $0 as! AXUIElement } ?? axApp
        found = axFind(root, session)
        if found != nil { break }
        usleep(800_000)
    }
    guard let el = found, let pt = axCenter(el) else {
        appendLog("claude: сессия «\(session)» не найдена в боковой панели")
        return "Не нашёл в Claude сессию \(session). Проверь, что она так называется и видна в списке слева. Заказ лежит в буфере."
    }
    click(at: pt)
    usleep(1_800_000)
    pressKey(9, cmd: true)
    usleep(500_000)
    pressKey(36)
    appendLog("claude: заказ отправлен в сессию «\(session)»")
    return nil
}

func connectHapp() -> Int32 {
    let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
    if !AXIsProcessTrustedWithOptions(opts) {
        appendLog("подключение Happ: нет разрешения «Универсальный доступ» для JARVIS")
        writeResult("no_permission")
        return 2
    }
    if happApp() == nil {
        runOpen(["-a", "Happ"])
        _ = waitFor(20) { happApp() != nil }
    }
    guard let app = happApp() else {
        appendLog("подключение Happ: Happ не запустился")
        writeResult("no_app")
        return 3
    }
    runOpen(["-a", "Happ"])
    app.activate(options: [.activateIgnoringOtherApps])
    guard waitFor(12, { happWindow(pid: app.processIdentifier) != nil }),
          let r = happWindow(pid: app.processIdentifier) else {
        appendLog("подключение Happ: окно Happ не найдено на экране")
        writeResult("no_window")
        return 4
    }
    usleep(800_000)
    var x = r.minX + 453 + (r.width - 453) / 2
    var y = r.minY + 209
    if r.width < 650 || r.height < 420 {
        x = r.minX + r.width * 0.771
        y = r.minY + r.height * 0.371
    }
    click(at: CGPoint(x: x, y: y))
    appendLog("подключение Happ: нажал кнопку в точке (\(Int(x)), \(Int(y))), окно \(r)")
    writeResult("clicked")
    return 0
}


// ───────── веб-вью: перетаскивание за шапку, клик без активации ─────────

final class HUDWebView: WKWebView {
    var dragZone: CGFloat = 0           // высота области сверху, за которую тащим окно
    var dragExcludeRight: CGFloat = 0    // справа в шапке кнопки — их не трогаем

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        if dragZone > 0 {
            let p = convert(event.locationInWindow, from: nil)
            let fromTop = isFlipped ? p.y : bounds.height - p.y
            if fromTop < dragZone && p.x < bounds.width - dragExcludeRight {
                window?.performDrag(with: event)
                return
            }
        }
        super.mouseDown(with: event)
    }
}

final class KeyPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

final class DashWindow: NSWindow {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { true }
}

/// Страница ядра, которая сама переподключается, пока ядро не поднялось.
final class Page: NSObject, WKNavigationDelegate {
    let web: HUDWebView
    let url: URL
    var attempts = 0
    var onLoad: (() -> Void)?

    init(path: String, handler: WKScriptMessageHandler, name: String, transparent: Bool) {
        let cfg = WKWebViewConfiguration()
        cfg.mediaTypesRequiringUserActionForPlayback = []
        cfg.userContentController.add(handler, name: name)
        web = HUDWebView(frame: .zero, configuration: cfg)
        url = URL(string: BASE + path)!
        super.init()
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        web.autoresizingMask = [.width, .height]
        if #available(macOS 13.3, *) { web.isInspectable = true }
        _ = transparent
    }

    func load() {
        web.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: 6))
    }

    func js(_ code: String) { web.evaluateJavaScript(code, completionHandler: nil) }

    func retry() {
        attempts += 1
        DispatchQueue.main.asyncAfter(deadline: .now() + (attempts < 40 ? 0.5 : 3)) { [weak self] in self?.load() }
    }

    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) { retry() }
    func webView(_ w: WKWebView, didFail n: WKNavigation!, withError e: Error) { retry() }
    func webView(_ w: WKWebView, didFinish n: WKNavigation!) { attempts = 0; onLoad?() }
    func webViewWebContentProcessDidTerminate(_ w: WKWebView) { load() }
}


// ───────── голосовое управление ─────────

final class VoiceControl: NSObject, AVSpeechSynthesizerDelegate, AVAudioPlayerDelegate {
    let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "ru-RU"))
    let engine = AVAudioEngine()
    let synth = AVSpeechSynthesizer()
    var request: SFSpeechAudioBufferRecognitionRequest?
    var task: SFSpeechRecognitionTask?
    var restartTimer: Timer?
    var articleTimer: Timer?
    var articleText = ""
    var generation = 0
    var enabled = true
    var speaking = false
    var externalSpeaking = false
    var afterSpeech: (() -> Void)?
    var countdown: DispatchWorkItem?
    var powerKind = "shutdown"
    var tempTriggers: [String] = []
    var tempUntil = Date.distantPast
    var pendingSay: [(String, String, [String: Any]?)] = []
    var pollStarted = false
    var greeted = false
    weak var app: AppDelegate?

    override init() {
        super.init()
        synth.delegate = self
    }

    // ── мост в интерфейс ──
    func jsAll(_ code: String) { DispatchQueue.main.async { self.app?.jsAll(code) } }
    func ui(_ state: String, _ text: String) {
        jsAll("window.jarvisVoice && window.jarvisVoice(\(jsStr(state)), \(jsStr(text)))")
    }

    func startPolling() {
        if pollStarted { return }
        pollStarted = true
        Timer.scheduledTimer(withTimeInterval: 2.5, repeats: true) { [weak self] _ in
            guard let self = self else { return }
            coreGet("/api/announce") { obj in
                if let t = obj["say"] as? String, !t.isEmpty {
                    self.pendingSay.append((t, (obj["emotion"] as? String) ?? "", obj["ui"] as? [String: Any]))
                }
            }
            if !self.speaking && !self.externalSpeaking && self.countdown == nil && !self.pendingSay.isEmpty {
                let (t, e, u) = self.pendingSay.removeFirst()
                if let u = u { self.app?.handleUI(u) }
                self.say(t, emotion: e, mirror: true)
            }
        }
    }

    func cleanText(_ t: String) -> String {
        let keep = CharacterSet.letters.union(.decimalDigits).union(CharacterSet(charactersIn: ":"))
        return t.components(separatedBy: keep.inverted).filter { !$0.isEmpty }.joined(separator: " ")
    }

    // ── запуск и разрешения ──
    func start() {
        startPolling()
        greet()
        SFSpeechRecognizer.requestAuthorization { status in
            DispatchQueue.main.async {
                guard status == .authorized else {
                    self.ui("off", "нет доступа к распознаванию речи")
                    appendLog("голос: нет разрешения на распознавание речи (\(status.rawValue))")
                    return
                }
                AVCaptureDevice.requestAccess(for: .audio) { ok in
                    DispatchQueue.main.async {
                        if ok { self.listen() } else {
                            self.ui("off", "нет доступа к микрофону")
                            appendLog("голос: нет доступа к микрофону")
                        }
                    }
                }
            }
        }
    }

    func greet() {
        if greeted { return }
        greeted = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 3.2) {
            coreGet("/api/greet") { [weak self] obj in
                guard let self = self, let t = obj["say"] as? String, !t.isEmpty else { return }
                if let p = obj["pet"] as? [String: Any], let c = p["cmd"] as? String { self.app?.pet.command(c) }
                self.say(t, emotion: (obj["emotion"] as? String) ?? "", mirror: false)
            }
        }
    }

    func setEnabled(_ on: Bool) {
        enabled = on
        UserDefaults.standard.set(on, forKey: "micOn")
        if on { listen() } else { stopListening(); ui("off", "выключен") }
        app?.refreshMenu()
    }

    func setExternalSpeaking(_ on: Bool) {
        externalSpeaking = on
        if on { stopListening() } else if !speaking { listen() }
    }

    // ── прослушивание ──
    var micWait: Timer?
    var noMicLogged = false

    func stopListening() {
        restartTimer?.invalidate(); restartTimer = nil
        micWait?.invalidate(); micWait = nil
        generation += 1
        task?.cancel(); task = nil
        request?.endAudio(); request = nil
        if engine.isRunning { engine.stop() }
        engine.inputNode.removeTap(onBus: 0)
    }

    func retry(after seconds: Double) {
        stopListening()
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in self?.listen() }
    }

    func listen() {
        stopListening()
        guard enabled, !speaking, !externalSpeaking else { return }
        guard let rec = recognizer, rec.isAvailable else {
            ui("off", "распознавание недоступно, пробую снова")
            retry(after: 5)
            return
        }
        // у Mac mini нет встроенного микрофона: без устройства ввода не слушаем (иначе AVAudioEngine падает)
        let input = engine.inputNode
        let hw = input.inputFormat(forBus: 0)
        let format = input.outputFormat(forBus: 0)
        guard AVCaptureDevice.default(for: .audio) != nil, hw.channelCount > 0, hw.sampleRate > 0,
              format.channelCount > 0, format.sampleRate > 0 else {
            if !noMicLogged { noMicLogged = true; appendLog("голос: микрофон не подключён — жду устройство") }
            ui("off", "микрофон не подключён — пиши команды текстом")
            micWait?.invalidate()
            micWait = Timer.scheduledTimer(withTimeInterval: 8, repeats: false) { [weak self] _ in self?.listen() }
            return
        }
        if noMicLogged { noMicLogged = false; appendLog("голос: микрофон появился") }
        let req = SFSpeechAudioBufferRecognitionRequest()
        req.shouldReportPartialResults = true
        req.contextualStrings = ["Джарвис", "Jarvis", "JARVIS", "Марк", "Марк ноль", "дашборд", "виджет"]
        if rec.supportsOnDeviceRecognition { req.requiresOnDeviceRecognition = true }
        request = req
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in req.append(buffer) }
        engine.prepare()
        do { try engine.start() } catch {
            ui("off", "микрофон недоступен")
            appendLog("голос: не стартовал микрофон: \(error)")
            retry(after: 5)
            return
        }
        let myGen = generation
        ui("on", countdown == nil ? "слушаю" : "скажи «отмена», чтобы остановить")
        task = rec.recognitionTask(with: req) { [weak self] result, error in
            DispatchQueue.main.async {
                guard let self = self, myGen == self.generation else { return }
                if let r = result {
                    if r.isFinal { appendLog("голос: услышал «\(r.bestTranscription.formattedString)»") }
                    self.heard(r.bestTranscription.formattedString)
                    if r.isFinal { self.listen(); return }
                }
                if error != nil { self.retry(after: 1) }
            }
        }
        restartTimer = Timer.scheduledTimer(withTimeInterval: 50, repeats: false) { [weak self] _ in self?.listen() }
    }

    // ── слово-активатор «Джарвис» ──
    var awakeUntil = Date.distantPast
    var lastWakeKey = ""
    let wakeWords = ["джарвис", "джарвиз", "джервис", "жарвис", "чарвис", "джарвас", "джарвес", "jarvis", "джарви",
                     "джаврис", "дарвис", "гарвис", "харвис", "жервис", "джарвиc", "jervis", "jarvi", "джорвис",
                     "джарва", "жарви", "чарви", "дарви", "джерви"]

    func wakeRange(_ t: String) -> Range<String.Index>? {
        var best: Range<String.Index>? = nil
        for w in wakeWords {
            guard let r = t.range(of: w, options: .backwards) else { continue }
            if best == nil || r.lowerBound > best!.lowerBound { best = r }
        }
        return best
    }

    func wake() {
        awakeUntil = Date().addingTimeInterval(8)
        NSSound(named: "Tink")?.play()
        jsAll("window.jarvisWake && window.jarvisWake()")
        app?.pet.js("window.petListen && petListen(true)")
        DispatchQueue.main.asyncAfter(deadline: .now() + 8.5) { [weak self] in
            guard let self = self else { return }
            if Date() >= self.awakeUntil { self.app?.pet.js("window.petListen && petListen(false)") }
        }
    }

    func sleepWake() {
        awakeUntil = Date.distantPast
        app?.pet.js("window.petListen && petListen(false)")
    }

    // ── разбор фраз ──
    func heard(_ raw: String) {
        let t = raw.lowercased().replacingOccurrences(of: "ё", with: "е")

        if countdown != nil {
            let w = Set(t.components(separatedBy: CharacterSet.letters.inverted).filter { !$0.isEmpty })
            if t.contains("отмен") || w.contains("стоп") || t.contains("не надо") || t.contains("передумал") {
                cancelPower()
            }
            return
        }

        let waiting = Date() < tempUntil && tempTriggers.contains { cleanText(t).contains($0) }

        var cmdT = t
        if let r = wakeRange(t) {
            let key = "\(generation):\(t.distance(from: t.startIndex, to: r.lowerBound))"
            if key != lastWakeKey { lastWakeKey = key; wake() }
            var idx = r.upperBound
            while idx < t.endIndex, t[idx].isLetter { idx = t.index(after: idx) }
            cmdT = String(t[idx...])
            awakeUntil = max(awakeUntil, Date().addingTimeInterval(8))
        } else if Date() >= awakeUntil && !waiting {
            ui("on", raw)
            return
        }
        cmdT = cmdT.trimmingCharacters(in: CharacterSet.letters.union(.decimalDigits).inverted)
        ui("on", cmdT.isEmpty ? "слушаю…" : cmdT)
        if cmdT.isEmpty { return }
        let words = Set(cmdT.components(separatedBy: CharacterSet.letters.inverted).filter { !$0.isEmpty })

        let off = cmdT.range(of: "(выключи|выключай|отключи|вырубай|выруби)(сь)?\\s+(мне\\s+|весь\\s+|этот\\s+)?(компьютер|мак|макбук|ноутбук)(\\s|$)",
                             options: .regularExpression) != nil
        let reboot = cmdT.range(of: "^(перезагрузи(сь)?|перезагрузка|ребут|перезапусти)(\\s+(компьютер|мак|макбук|ноутбук|систему))?$",
                                options: .regularExpression) != nil
        if off || reboot {
            articleTimer?.invalidate()
            appendLog("голос: команда «\(raw)»")
            sleepWake()
            beginPower(reboot ? "restart" : "shutdown")
            return
        }
        // всё остальное — в ядро, когда договоришь
        articleText = cmdT
        articleTimer?.invalidate()
        let quick = words.contains("вставь") || words.contains("пауза") || cmdT.hasSuffix("громче") || cmdT.hasSuffix("тише")
        articleTimer = Timer.scheduledTimer(withTimeInterval: quick ? 0.9 : 1.6, repeats: false) { [weak self] _ in
            guard let self = self else { return }
            let text = self.articleText
            self.sleepWake()
            appendLog("голос: фраза «\(text)» → ядро")
            self.jsAll("window.jarvisHeard && window.jarvisHeard(\(jsStr(text)))")
            self.runCommand(["cmd": "text", "text": text])
        }
    }

    func runCommand(_ cmd: [String: Any]) {
        stopListening()
        corePost("/api/voice", cmd, timeout: 30) { [weak self] obj in
            guard let self = self else { return }
            var phrase = (obj["say"] as? String) ?? (obj.isEmpty ? "Ядро не ответило. Попробуй ещё раз." : "")
            let emotion = (obj["emotion"] as? String) ?? ""
            if let exp = obj["expect"] as? [String], !exp.isEmpty {
                self.tempTriggers = exp.map { $0.lowercased() }
                self.tempUntil = Date().addingTimeInterval(90)
            } else {
                self.tempUntil = Date.distantPast
            }
            if let u = obj["ui"] as? [String: Any] {
                if (u["restart"] as? Bool) == true { phrase = ""; self.beginPower("restart"); return }
                self.app?.handleUI(u)
            }
            if let p = obj["pet"] as? [String: Any], let c = p["cmd"] as? String { self.app?.pet.command(c) }
            if phrase.isEmpty { self.listen() } else { self.say(phrase, emotion: emotion) }
            if let session = obj["claude_session"] as? String {
                DispatchQueue.global(qos: .userInitiated).async {
                    if let err = sendToClaude(session: session) {
                        DispatchQueue.main.async { self.say(err, emotion: "concerned") }
                    }
                }
            }
        }
    }

    // ── речь: нейросеть через ядро → WAV; уровень громкости — в реактор ──
    var player: AVAudioPlayer?
    var meterTimer: Timer?

    lazy var maleSystemVoice: AVSpeechSynthesisVoice? = {
        let ru = AVSpeechSynthesisVoice.speechVoices().filter { $0.language.hasPrefix("ru") }
        let male = ru.filter { $0.gender == .male || $0.name.contains("Yuri") || $0.name.contains("Юрий") }
        return male.sorted { $0.quality.rawValue > $1.quality.rawValue }.first
    }()

    func say(_ text: String, emotion: String = "", mirror: Bool = false, then: (() -> Void)? = nil) {
        stopListening()
        speaking = true
        afterSpeech = then
        let showText = { [weak self] in
            guard let self = self else { return }
            self.jsAll("window.jarvisSays && window.jarvisSays(\(jsStr(text)), \(jsStr(emotion)))")
            if mirror { self.app?.pet.bubble(text) }
        }
        corePost("/api/tts", ["text": text, "emotion": emotion], timeout: 40) { [weak self] obj in
            guard let self = self else { return }
            if let path = obj["path"] as? String, let p = try? AVAudioPlayer(contentsOf: URL(fileURLWithPath: path)) {
                self.player = p
                p.delegate = self
                p.isMeteringEnabled = true
                p.prepareToPlay()
                p.play()
                showText()
                self.startMeter()
                return
            }
            if let v = self.maleSystemVoice {
                let u = AVSpeechUtterance(string: text)
                u.voice = v
                u.rate = 0.47
                u.pitchMultiplier = 0.85
                self.synth.speak(u)
                showText()
                return
            }
            appendLog("голос: синтез не ответил — показываю текстом")
            showText()
            DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { self.finishedSpeaking() }
        }
    }

    func startMeter() {
        meterTimer?.invalidate()
        meterTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 30, repeats: true) { [weak self] _ in
            guard let self = self, let p = self.player, p.isPlaying else { return }
            p.updateMeters()
            let db = p.averagePower(forChannel: 0)
            let lvl = max(0, min(1, pow(10, db / 20) * 2.6))
            let s = String(format: "%.3f", lvl)
            self.app?.jsAll("window.jarvisLevel && window.jarvisLevel(\(s))")
            self.app?.pet.js("window.petLevel && petLevel(\(s))")
        }
    }

    func finishedSpeaking() {
        meterTimer?.invalidate(); meterTimer = nil
        app?.jsAll("window.jarvisLevel && window.jarvisLevel(0)")
        speaking = false
        let next = afterSpeech
        afterSpeech = nil
        if let next = next { next() } else { listen() }
    }

    func audioPlayerDidFinishPlaying(_ p: AVAudioPlayer, successfully flag: Bool) {
        DispatchQueue.main.async { self.finishedSpeaking() }
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish u: AVSpeechUtterance) {
        DispatchQueue.main.async { self.finishedSpeaking() }
    }

    // ── выключение и перезагрузка: пять секунд на «отмена» ──
    func beginPower(_ kind: String) {
        if countdown != nil { return }
        powerKind = kind
        let w = DispatchWorkItem { [weak self] in self?.finalPower() }
        countdown = w
        app?.expandDashboard()
        jsAll("window.jarvisVoiceShutdown && window.jarvisVoiceShutdown(\(jsStr(kind)))")
        let phrase = kind == "restart"
            ? "Перезагружаю систему. Пять секунд, чтобы сказать «отмена»."
            : "Как скажешь. Запускаю протокол «Чистый лист». Глушу реактор и закрываю все системы."
        say(phrase, emotion: "concerned") { [weak self] in
            guard let self = self, self.countdown != nil else { return }
            self.jsAll("window.jarvisVoiceCountdown && window.jarvisVoiceCountdown(5)")
            self.listen()
            DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: w)
        }
    }

    func cancelPower() {
        countdown?.cancel()
        countdown = nil
        appendLog("голос: \(powerKind) отменено")
        jsAll("window.jarvisVoiceCancel && window.jarvisVoiceCancel()")
        say(powerKind == "restart" ? "Перезагрузка отменена. Работаем дальше." : "Отменяю. Реактор остаётся в строю.", emotion: "happy")
    }

    func finalPower() {
        guard countdown != nil else { return }
        countdown = nil
        jsAll("window.jarvisVoiceBye && window.jarvisVoiceBye()")
        let kind = powerKind
        say(kind == "restart" ? "Перезагружаю. Вернусь через минуту." : "Доброй ночи. Буду ждать.", emotion: "warm") {
            corePost(kind == "restart" ? "/api/restart" : "/api/shutdown", [:])
        }
    }
}


// ───────── Марк-0: живёт поверх всех окон и лазает по экрану ─────────

enum Orient: String { case floor, wallL, wallR, ceil, air }
enum Act: String { case idle, walk, fly, fall, drag, sit, sleep, dizzy, peek, chase }

struct Win { let id: Int; let rect: NSRect; let owner: String }

final class PetController: NSObject, WKScriptMessageHandler {
    weak var app: AppDelegate?
    var panel: NSPanel?
    var page: Page?
    var timer: Timer?
    let size: CGFloat = 660
    let half: CGFloat = 330
    let h2: CGFloat = 40          // от центра робота до подошв
    let w2: CGFloat = 32          // половина ширины робота

    var pos = CGPoint.zero
    var vel = CGVector.zero
    var orient = Orient.floor
    var act = Act.idle
    var dir: CGFloat = 1          // направление движения вдоль поверхности
    var face: CGFloat = 1
    var surfaceWin: Int? = nil    // id окна, на котором стоим
    var winOwner = ""
    var nextDecision = Date()
    var walkTarget: CGFloat? = nil
    var speed: CGFloat = 46
    // полёт
    var flyFrom = CGPoint.zero, flyTo = CGPoint.zero, flyCtl = CGPoint.zero
    var flyT: CGFloat = 0, flyDur: CGFloat = 1
    var flyLand: (Orient, Int?)? = nil
    var chaseUntil = Date.distantPast
    // окна
    var windows: [Win] = []
    var lastWinScan = Date.distantPast
    // мышь
    var hitRects: [NSRect] = []
    var pressed = false, dragging = false
    var pressPoint = NSPoint.zero
    var grabOffset = CGVector.zero
    var lastMouse = NSPoint.zero
    var mouseVel = CGVector.zero
    var strokeSent = Date.distantPast
    var menuOpen = false
    var busyUntil = Date.distantPast
    var hidden = false
    var lastPose = ""
    var lastVis = ""
    var frame = 0
    var lastTick = Date()
    var dizzyUntil = Date.distantPast

    var voiceOn: Bool {
        get { UserDefaults.standard.object(forKey: "petVoice") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "petVoice"); js("window.petVoice && petVoice(\(newValue))") }
    }
    var isShown: Bool { panel?.isVisible ?? false }

    func show() {
        if panel == nil { build() }
        panel?.orderFrontRegardless()
        UserDefaults.standard.set(true, forKey: "petVisible")
        hidden = false
    }

    func hide() {
        panel?.orderOut(nil)
        UserDefaults.standard.set(false, forKey: "petVisible")
    }

    func toggle() { isShown ? hide() : show() }

    func js(_ code: String) { page?.js(code) }

    func bubble(_ text: String) { js("window.petBubble && petBubble(\(jsStr(text)))") }

    var screenVF: NSRect {
        let s = NSScreen.screens.first { $0.frame.contains(pos) } ?? NSScreen.main ?? NSScreen.screens.first!
        return s.visibleFrame
    }

    func build() {
        let vf = (NSScreen.main ?? NSScreen.screens.first!).visibleFrame
        pos = CGPoint(x: vf.maxX - 160, y: vf.minY + h2)
        let p = NSPanel(contentRect: NSRect(x: pos.x - half, y: pos.y - half, width: size, height: size),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = false
        p.level = .floating
        p.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        p.hidesOnDeactivate = false
        p.isReleasedWhenClosed = false
        p.isRestorable = false
        p.ignoresMouseEvents = true
        p.acceptsMouseMovedEvents = true
        let pg = Page(path: "/pet.html?voice=\(voiceOn ? 1 : 0)", handler: self, name: "pet", transparent: true)
        pg.web.frame = NSRect(x: 0, y: 0, width: size, height: size)
        p.contentView = pg.web
        pg.onLoad = { [weak self] in self?.lastPose = ""; self?.sendPose(force: true) }
        pg.load()
        panel = p
        page = pg
        timer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60, repeats: true) { [weak self] _ in self?.tick() }
        RunLoop.main.add(timer!, forMode: .common)
        nextDecision = Date().addingTimeInterval(4)
    }

    // ── окна, по которым можно гулять ──
    func scanWindows() {
        lastWinScan = Date()
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] else { return }
        let primaryH = NSScreen.screens.first?.frame.height ?? 0
        let me = ProcessInfo.processInfo.processIdentifier
        let vf = screenVF
        var out: [Win] = []
        for w in list {
            guard let layer = (w[kCGWindowLayer as String] as? NSNumber)?.intValue, layer == 0,
                  let pid = (w[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value, pid != me,
                  let id = (w[kCGWindowNumber as String] as? NSNumber)?.intValue,
                  let bd = w[kCGWindowBounds as String] as? NSDictionary,
                  let r = CGRect(dictionaryRepresentation: bd as CFDictionary) else { continue }
            let alpha = (w[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 1
            if alpha < 0.3 || r.width < 220 || r.height < 120 { continue }
            let cocoa = NSRect(x: r.minX, y: primaryH - r.maxY, width: r.width, height: r.height)
            if cocoa.maxY > vf.maxY - 30 || cocoa.maxY < vf.minY + 140 { continue }     // верх окна у меню-бара или у пола — не годится
            if cocoa.width > vf.width * 0.97 && cocoa.height > vf.height * 0.9 { continue }
            out.append(Win(id: id, rect: cocoa, owner: (w[kCGWindowOwnerName as String] as? String) ?? ""))
            if out.count >= 12 { break }
        }
        windows = out
    }

    func win(_ id: Int?) -> Win? { guard let id = id else { return nil }; return windows.first { $0.id == id } }

    // ── главный цикл: 60 кадров в секунду ──
    func tick() {
        guard let p = panel, p.isVisible else { return }
        let now = Date()
        let dt = CGFloat(min(0.05, now.timeIntervalSince(lastTick)))
        lastTick = now
        frame += 1
        let mouse = NSEvent.mouseLocation
        let mv = CGVector(dx: mouse.x - lastMouse.x, dy: mouse.y - lastMouse.y)
        mouseVel = CGVector(dx: mouseVel.dx * 0.7 + mv.dx / max(dt, 0.001) * 0.3, dy: mouseVel.dy * 0.7 + mv.dy / max(dt, 0.001) * 0.3)
        lastMouse = mouse
        if now.timeIntervalSince(lastWinScan) > 0.4 { followWindows() }

        let buttons = NSEvent.pressedMouseButtons & 1 == 1
        // перетаскивание
        if pressed {
            if !buttons {
                pressed = false
                if dragging {
                    dragging = false
                    let v = CGVector(dx: max(-2600, min(2600, mouseVel.dx)), dy: max(-2600, min(2600, mouseVel.dy)))
                    startFall(v)
                }
            } else if !dragging && hypot(mouse.x - pressPoint.x, mouse.y - pressPoint.y) > 6 {
                dragging = true
                js("window.petDragged && petDragged()")
                js("window.petMenuClose && petMenuClose()")
                setAct(.drag, .air)
            }
        }
        if dragging {
            let target = CGPoint(x: mouse.x + grabOffset.dx, y: mouse.y + grabOffset.dy)
            pos.x += (target.x - pos.x) * 0.45
            pos.y += (target.y - pos.y) * 0.45
            let sw = max(-38, min(38, -mouseVel.dx * 0.03))
            if frame % 2 == 0 { js("window.petSwing && petSwing(\(String(format: "%.1f", sw)))") }
        } else if !menuOpen && now > busyUntil {
            step(dt, now)
        }

        // окно следует за роботом
        p.setFrameOrigin(NSPoint(x: round(pos.x - half), y: round(pos.y - half)))

        // мышь: клики только по роботу и облаку
        let local = NSPoint(x: mouse.x - p.frame.minX, y: mouse.y - p.frame.minY)
        let over = hitRects.contains { $0.contains(local) }
        p.ignoresMouseEvents = !(over || pressed || dragging)
        if menuOpen && buttons && !over {
            js("window.petMenuClose && petMenuClose()")
            menuOpen = false
        }
        // поглаживание: курсор движется над роботом без нажатия
        if over && !buttons && !dragging, let bot = hitRects.first, bot.contains(local) {
            let d = hypot(mv.dx, mv.dy)
            if d > 0.5 && now.timeIntervalSince(strokeSent) > 0.066 {
                strokeSent = now
                js("window.petStroke && petStroke(\(String(format: "%.3f", min(1, d / 18))))")
            }
        }
        // глаза следят за курсором
        if frame % 4 == 0 {
            js("window.petMouse && petMouse(\(Int(mouse.x - pos.x)), \(Int(mouse.y - pos.y)))")
        }
        if frame % 30 == 0 { sendPose(force: false) }
    }

    func step(_ dt: CGFloat, _ now: Date) {
        let vf = screenVF
        // сон, когда за компом никого нет
        let idle = userIdleSeconds()
        if idle > 600 && act != .sleep && act != .fly && act != .fall {
            if orient == .floor { setAct(.sleep, .floor) }
            else { sleepAfterLanding = true; startFall(.zero) }
        } else if act == .sleep && idle < 3 && !manualSleep {
            setAct(.idle, orient)
            js("window.petCmd && petCmd('happy')")
            nextDecision = now.addingTimeInterval(2)
        }

        switch act {
        case .fly, .chase:
            flyT += dt / flyDur
            let t = min(1, flyT)
            let e = t < 0.5 ? 2 * t * t : 1 - pow(-2 * t + 2, 2) / 2
            let a = (1 - e) * (1 - e), b = 2 * (1 - e) * e, c = e * e
            let np = CGPoint(x: a * flyFrom.x + b * flyCtl.x + c * flyTo.x, y: a * flyFrom.y + b * flyCtl.y + c * flyTo.y)
            if abs(np.x - pos.x) > 0.3 { face = np.x > pos.x ? 1 : -1 }
            pos = np
            if act == .chase && t >= 1 {
                // висим у курсора и разглядываем его
                let m = NSEvent.mouseLocation
                let tgt = CGPoint(x: m.x + (m.x > vf.midX ? -90 : 90), y: m.y - 10)
                pos.x += (tgt.x - pos.x) * 0.05
                pos.y += (tgt.y - pos.y) * 0.05
                face = m.x > pos.x ? 1 : -1
                if now > chaseUntil { flyToFloor() }
            } else if t >= 1 {
                land()
            }
        case .fall:
            vel.dy -= 2300 * dt
            vel.dx *= 0.995
            let feetBefore = pos.y - h2
            pos.x += vel.dx * dt
            pos.y += vel.dy * dt
            if pos.x < vf.minX + w2 { pos.x = vf.minX + w2; vel.dx = abs(vel.dx) * 0.4 }
            if pos.x > vf.maxX - w2 { pos.x = vf.maxX - w2; vel.dx = -abs(vel.dx) * 0.4 }
            if pos.y > vf.maxY - h2 { pos.y = vf.maxY - h2; vel.dy = min(0, vel.dy) }
            let feet = pos.y - h2
            if vel.dy < 0 {
                for w in windows where feetBefore >= w.rect.maxY && feet <= w.rect.maxY
                    && pos.x > w.rect.minX + 18 && pos.x < w.rect.maxX - 18 {
                    pos.y = w.rect.maxY + h2
                    landed(onWin: w, impact: -vel.dy)
                    return
                }
            }
            if feet <= vf.minY {
                pos.y = vf.minY + h2
                landed(onWin: nil, impact: -vel.dy)
            }
        case .walk:
            walk(dt, vf, now)
        case .dizzy:
            if now > dizzyUntil { setAct(.idle, orient); nextDecision = now.addingTimeInterval(1.5) }
        case .peek:
            if now > nextDecision {
                pos.x = max(vf.minX + w2, min(vf.maxX - w2, pos.x))
                setAct(.idle, .floor)
                nextDecision = now.addingTimeInterval(2)
            }
        case .idle, .sit:
            if now > nextDecision { decide(vf, now) }
        case .sleep, .drag:
            break
        }
        clampToSurface(vf)
    }

    /// Держим робота на своей поверхности (окно могло сдвинуться, экран — смениться).
    func clampToSurface(_ vf: NSRect) {
        guard act != .fly && act != .chase && act != .fall && act != .drag && act != .peek else { return }
        switch orient {
        case .floor:
            if let w = win(surfaceWin) {
                pos.y = w.rect.maxY + h2 - (act == .sit || act == .sleep ? 0 : 0)
            } else if surfaceWin != nil {
                startFall(.zero)
            } else {
                pos.y = vf.minY + h2
                pos.x = max(vf.minX + w2, min(vf.maxX - w2, pos.x))
            }
        case .wallL: pos.x = vf.minX + h2; pos.y = max(vf.minY + w2, min(vf.maxY - w2, pos.y))
        case .wallR: pos.x = vf.maxX - h2; pos.y = max(vf.minY + w2, min(vf.maxY - w2, pos.y))
        case .ceil: pos.y = vf.maxY - h2; pos.x = max(vf.minX + w2, min(vf.maxX - w2, pos.x))
        case .air: break
        }
    }

    func followWindows() {
        let old = win(surfaceWin)
        scanWindows()
        guard let id = surfaceWin else { return }
        if let w = win(id), let o = old {
            pos.x += w.rect.minX - o.rect.minX                   // окно двигают — едем вместе с ним
            if pos.x < w.rect.minX + 6 || pos.x > w.rect.maxX - 6 { startFall(.zero) }
        } else if old != nil || win(id) == nil {
            surfaceWin = nil
            if act != .fly && act != .drag { startFall(.zero) }
            js("window.petCmd && petCmd('confused')")
        }
    }

    // ── ходьба: по полу, по окну, по стенам и потолку ──
    func walk(_ dt: CGFloat, _ vf: NSRect, _ now: Date) {
        let d = speed * dt * dir
        switch orient {
        case .floor:
            pos.x += d
            face = dir
            if let w = win(surfaceWin) {
                if pos.x < w.rect.minX + 14 || pos.x > w.rect.maxX - 14 {
                    if Double.random(in: 0...1) < 0.45 { surfaceWin = nil; startFall(CGVector(dx: dir * 120, dy: 260)) }
                    else { dir = -dir; pos.x += 2 * d }
                    return
                }
            } else {
                if pos.x <= vf.minX + w2 {
                    pos.x = vf.minX + w2
                    if walkTarget == -1 || Double.random(in: 0...1) < 0.5 { climbOnto(.wallL, vf); return }
                    dir = 1
                } else if pos.x >= vf.maxX - w2 {
                    pos.x = vf.maxX - w2
                    if walkTarget == -1 || Double.random(in: 0...1) < 0.5 { climbOnto(.wallR, vf); return }
                    dir = -1
                }
            }
            if let t = walkTarget, t >= 0, (dir > 0 && pos.x >= t) || (dir < 0 && pos.x <= t) {
                walkTarget = nil
                setAct(surfaceWin != nil && Double.random(in: 0...1) < 0.6 ? .sit : .idle, .floor)
                nextDecision = now.addingTimeInterval(Double.random(in: 3...9))
            }
        case .wallL, .wallR:
            pos.y += d
            face = orient == .wallL ? -dir : dir
            if pos.y >= vf.maxY - w2 {
                if Double.random(in: 0...1) < 0.55 {
                    let left = orient == .wallL
                    orient = .ceil
                    pos.y = vf.maxY - h2
                    pos.x = left ? vf.minX + w2 + 2 : vf.maxX - w2 - 2
                    dir = left ? 1 : -1
                    face = -dir
                    sendPose(force: true)
                } else { dir = -1 }
            } else if pos.y <= vf.minY + w2 {
                let left = orient == .wallL
                orient = .floor
                pos.y = vf.minY + h2
                pos.x = left ? vf.minX + w2 : vf.maxX - w2
                dir = left ? 1 : -1
                face = dir
                setAct(.idle, .floor)
                nextDecision = now.addingTimeInterval(Double.random(in: 2...5))
            } else if Double.random(in: 0...1) < 0.0009 {
                startFall(CGVector(dx: orient == .wallL ? 160 : -160, dy: 0))         // сорвался!
                js("window.petCmd && petCmd('confused')")
            }
        case .ceil:
            pos.x += d
            face = -dir
            if pos.x <= vf.minX + w2 || pos.x >= vf.maxX - w2 {
                let left = pos.x <= vf.minX + w2
                orient = left ? .wallL : .wallR
                pos.x = left ? vf.minX + h2 : vf.maxX - h2
                pos.y = vf.maxY - w2
                dir = -1
                face = orient == .wallL ? -dir : dir
                sendPose(force: true)
            } else if Double.random(in: 0...1) < 0.0012 {
                startFall(CGVector(dx: 0, dy: -50))                                    // отпустил потолок
            }
        case .air:
            setAct(.idle, .floor)
        }
    }
    var sleepAfterLanding = false
    var manualSleep = false

    func climbOnto(_ o: Orient, _ vf: NSRect) {
        orient = o
        pos.x = o == .wallL ? vf.minX + h2 : vf.maxX - h2
        pos.y = vf.minY + w2 + 2
        dir = 1
        face = o == .wallL ? -1 : 1
        walkTarget = nil
        setAct(.walk, o)
    }

    // ── решения: что делать дальше ──
    func decide(_ vf: NSRect, _ now: Date) {
        let r = Double.random(in: 0...1)
        switch orient {
        case .floor where surfaceWin != nil:
            if r < 0.35 { startWalk(vf, onWin: true) }
            else if r < 0.55 { setAct(.sit, .floor); nextDecision = now.addingTimeInterval(Double.random(in: 6...14)) }
            else if r < 0.75 { flyToWindow() ?? flyToFloor() }
            else if r < 0.9 { surfaceWin = nil; startFall(CGVector(dx: Double.random(in: 0...1) < 0.5 ? -140 : 140, dy: 300)) }
            else { flyToFloor() }
        case .floor:
            if r < 0.34 { startWalk(vf, onWin: false) }
            else if r < 0.46 { goToWall(vf) }
            else if r < 0.64 { flyToWindow() ?? startWalk(vf, onWin: false) }
            else if r < 0.70 { chaseCursor() }
            else if r < 0.75 { peek(vf) }
            else if r < 0.82 { flyLoop(vf) }
            else { setAct(.idle, .floor); nextDecision = now.addingTimeInterval(Double.random(in: 4...10)) }
        case .wallL, .wallR, .ceil:
            dir = Double.random(in: 0...1) < 0.5 ? 1 : -1
            setAct(.walk, orient)
        case .air:
            flyToFloor()
        }
    }

    func startWalk(_ vf: NSRect, onWin: Bool) {
        var lo = vf.minX + w2 + 4, hi = vf.maxX - w2 - 4
        if onWin, let w = win(surfaceWin) { lo = w.rect.minX + 20; hi = w.rect.maxX - 20 }
        let t = CGFloat.random(in: lo...max(lo + 1, hi))
        if abs(t - pos.x) < 40 { setAct(.idle, orient); nextDecision = Date().addingTimeInterval(3); return }
        walkTarget = t
        dir = t > pos.x ? 1 : -1
        speed = CGFloat.random(in: 38...58)
        setAct(.walk, .floor)
    }

    func goToWall(_ vf: NSRect) {
        dir = pos.x - vf.minX < vf.maxX - pos.x ? -1 : 1
        walkTarget = -1
        speed = 52
        setAct(.walk, .floor)
    }

    @discardableResult
    func flyToWindow() -> Void? {
        let cands = windows.prefix(6).filter { $0.id != surfaceWin }
        guard let w = cands.randomElement() else { return nil }
        let x = CGFloat.random(in: (w.rect.minX + 30)...max(w.rect.minX + 31, w.rect.maxX - 30))
        flyTo(CGPoint(x: x, y: w.rect.maxY + h2), land: (.floor, w.id))
        return ()
    }

    func flyToFloor() {
        let vf = screenVF
        let x = max(vf.minX + 60, min(vf.maxX - 60, pos.x + CGFloat.random(in: -260...260)))
        flyTo(CGPoint(x: x, y: vf.minY + h2), land: (.floor, nil))
    }

    func flyLoop(_ vf: NSRect) {
        let p = CGPoint(x: CGFloat.random(in: (vf.minX + 120)...(vf.maxX - 120)), y: CGFloat.random(in: (vf.midY)...(vf.maxY - 120)))
        flyTo(p, land: nil)
    }

    func flyTo(_ p: CGPoint, land: (Orient, Int?)?, chase: Bool = false) {
        flyFrom = pos
        flyTo = p
        let dist = hypot(p.x - pos.x, p.y - pos.y)
        let arc = min(260, dist * 0.35 + 60)
        flyCtl = CGPoint(x: (pos.x + p.x) / 2, y: max(pos.y, p.y) + arc)
        let vf = screenVF
        flyCtl.y = min(flyCtl.y, vf.maxY - 30)
        flyT = 0
        flyDur = max(0.8, dist / 420)
        flyLand = land
        surfaceWin = nil
        face = p.x > pos.x ? 1 : -1
        setAct(chase ? .chase : .fly, .air)
    }

    func chaseCursor() {
        let m = NSEvent.mouseLocation
        let vf = screenVF
        flyTo(CGPoint(x: m.x + (m.x > vf.midX ? -90 : 90), y: m.y - 10), land: nil, chase: true)
        chaseUntil = Date().addingTimeInterval(Double.random(in: 4...7))
    }

    func peek(_ vf: NSRect) {
        let left = pos.x - vf.minX < vf.maxX - pos.x
        pos.x = left ? vf.minX - 18 : vf.maxX + 18
        face = left ? 1 : -1
        setAct(.peek, .floor)
        nextDecision = Date().addingTimeInterval(5)
    }

    func land() {
        if let l = flyLand {
            if let id = l.1, let w = win(id) {
                surfaceWin = id
                winOwner = w.owner
                pos.y = w.rect.maxY + h2
                setAct(Double.random(in: 0...1) < 0.55 ? .sit : .idle, .floor)
            } else if l.1 != nil {
                startFall(.zero)                     // окно пропало, пока летели
                return
            } else {
                surfaceWin = nil
                setAct(.idle, l.0)
            }
            nextDecision = Date().addingTimeInterval(Double.random(in: 3...8))
        } else {
            flyToFloor()
        }
    }

    func startFall(_ v: CGVector) {
        vel = v
        surfaceWin = nil
        setAct(.fall, .air)
    }

    func landed(onWin w: Win?, impact: CGFloat) {
        vel = .zero
        surfaceWin = w?.id
        winOwner = w?.owner ?? ""
        if sleepAfterLanding {
            sleepAfterLanding = false
            setAct(.sleep, .floor)
        } else if impact > 1500 {
            dizzyUntil = Date().addingTimeInterval(2.6)
            setAct(.dizzy, .floor)
        } else {
            setAct(.idle, .floor)
            nextDecision = Date().addingTimeInterval(Double.random(in: 1.5...4))
        }
    }

    func setAct(_ a: Act, _ o: Orient) {
        act = a
        orient = o
        sendPose(force: true)
    }

    func sendPose(force: Bool) {
        guard let p = panel else { return }
        let vf = screenVF
        let vx0 = vf.minX - p.frame.minX, vy0 = p.frame.maxY - vf.maxY
        let vis = "[\(Int(vx0)),\(Int(vy0)),\(Int(vx0 + vf.width)),\(Int(vy0 + vf.height))]"
        let key = "\(orient.rawValue)|\(act.rawValue)|\(face > 0 ? 1 : -1)"
        if !force && key == lastPose && vis == lastVis { return }
        if key == lastPose && vis != lastVis && !force && frame % 60 != 0 { return }
        lastPose = key
        lastVis = vis
        let app = act == .sit && !winOwner.isEmpty ? ",app:\(jsStr(winOwner))" : ""
        js("window.petPose && petPose({o:'\(orient.rawValue)',a:'\(act.rawValue)',f:\(face > 0 ? 1 : -1),vis:\(vis)\(app)})")
    }

    // ── команды от Джарвиса и из облака ──
    func command(_ c: String) {
        if !isShown && c != "hide" { show() }
        let vf = screenVF
        switch c {
        case "come":
            let m = NSEvent.mouseLocation
            flyTo(CGPoint(x: m.x + (m.x > vf.midX ? -80 : 80), y: m.y - 20), land: nil, chase: true)
            chaseUntil = Date().addingTimeInterval(6)
        case "fly": flyLoop(vf)
        case "wall": orient == .floor && surfaceWin == nil ? goToWall(vf) : flyTo(CGPoint(x: vf.minX + 80, y: vf.minY + h2), land: (.floor, nil))
        case "ceiling":
            climbOnto(pos.x < vf.midX ? .wallL : .wallR, vf); pos.y = vf.maxY - w2 - 4
        case "sleep":
            manualSleep = true
            if orient == .floor && act != .fly && act != .fall { setAct(.sleep, .floor) }
            else { sleepAfterLanding = true; startFall(.zero) }
        case "wake":
            manualSleep = false
            sleepAfterLanding = false
            if act == .sleep { setAct(.idle, orient) }
            js("window.petCmd && petCmd('happy')")
        case "hide":
            peek(vf)
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in self?.hide() }
        case "show": show(); flyToFloor()
        case "dance", "flip", "salute", "spin", "scan", "wave", "happy", "sad", "confused", "menu":
            js("window.petCmd && petCmd('\(c)')")
        default: break
        }
        nextDecision = Date().addingTimeInterval(4)
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let cmd = body["cmd"] as? String else { return }
        switch cmd {
        case "hit":
            if let rs = body["rects"] as? [[Double]] {
                hitRects = rs.compactMap { r in
                    r.count == 4 ? NSRect(x: r[0], y: Double(size) - r[1] - r[3], width: r[2], height: r[3]) : nil
                }
            }
        case "press":
            pressed = true
            pressPoint = NSEvent.mouseLocation
            grabOffset = CGVector(dx: pos.x - pressPoint.x, dy: pos.y - pressPoint.y)
        case "menu":
            menuOpen = (body["open"] as? Bool) ?? false
            if menuOpen && (act == .walk || act == .fly) { setAct(.idle, orient == .air ? .floor : orient) }
        case "busy":
            let ms = (body["ms"] as? NSNumber)?.doubleValue ?? 1500
            busyUntil = Date().addingTimeInterval(ms / 1000)
        case "speaking":
            app?.voice.setExternalSpeaking((body["on"] as? Bool) ?? false)
        case "do":
            if let w = body["what"] as? String { command(w) }
        case "action":
            if let name = body["name"] as? String { app?.performAction(name) }
        case "open":
            app?.expandDashboard()
        default: break
        }
    }
}


// ───────── виджет на рабочем столе ─────────

final class WidgetController: NSObject {
    weak var app: AppDelegate?
    var panel: KeyPanel?
    var page: Page?
    let size = NSSize(width: 352, height: 520)

    var enabled: Bool {
        get { UserDefaults.standard.object(forKey: "widgetOn") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "widgetOn") }
    }
    var pinned: Bool {
        get { UserDefaults.standard.bool(forKey: "widgetPinned") }
        set { UserDefaults.standard.set(newValue, forKey: "widgetPinned"); applyLevel() }
    }
    var isShown: Bool { panel?.isVisible ?? false }

    func build(handler: WKScriptMessageHandler) {
        let vf = (NSScreen.main ?? NSScreen.screens.first!).visibleFrame
        let p = KeyPanel(contentRect: NSRect(x: vf.maxX - size.width - 24, y: vf.maxY - size.height - 24,
                                             width: size.width, height: size.height),
                         styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = false
        p.hidesOnDeactivate = false
        p.isReleasedWhenClosed = false
        p.isRestorable = false
        p.isMovableByWindowBackground = false
        let pg = Page(path: "/widget.html", handler: handler, name: "jarvis", transparent: true)
        pg.web.frame = NSRect(origin: .zero, size: size)
        pg.web.dragZone = 40
        pg.web.dragExcludeRight = 118
        p.contentView = pg.web
        p.setFrameAutosaveName("JARVISWidget")
        pg.onLoad = { [weak self] in
            guard let self = self else { return }
            self.page?.js("window.jarvisPin && jarvisPin(\(self.pinned))")
        }
        pg.load()
        panel = p
        page = pg
        applyLevel()
    }

    func applyLevel() {
        guard let p = panel else { return }
        if pinned {
            p.level = .floating
            p.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .fullScreenAuxiliary]
        } else {
            p.level = NSWindow.Level(rawValue: Int(CGWindowLevelForKey(.desktopIconWindow)) + 1)
            p.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        }
        page?.js("window.jarvisPin && jarvisPin(\(pinned))")
    }

    func show() {
        guard let p = panel else { return }
        p.alphaValue = 0
        p.orderFrontRegardless()
        page?.js("window.jarvisMode && jarvisMode('widget')")
        NSAnimationContext.runAnimationGroup { ctx in ctx.duration = 0.35; p.animator().alphaValue = 1 }
    }

    func hide() { panel?.orderOut(nil) }
}


// ───────── приложение ─────────

final class AppDelegate: NSObject, NSApplicationDelegate, WKScriptMessageHandler {
    let voice = VoiceControl()
    let pet = PetController()
    let widget = WidgetController()
    var dash: DashWindow!
    var dashPage: Page!
    var server: Process?
    var status: NSStatusItem?
    var hotKeyRef: EventHotKeyRef?
    var voiceStarted = false

    func applicationWillFinishLaunching(_ notification: Notification) {
        UserDefaults.standard.set(false, forKey: "NSQuitAlwaysKeepsWindows")
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        voice.app = self
        pet.app = self
        widget.app = self
        voice.enabled = UserDefaults.standard.object(forKey: "micOn") as? Bool ?? true
        buildMenu()
        startServer()
        buildDashboard()
        widget.build(handler: self)
        buildStatusItem()
        registerHotkey()
        expandDashboard()
        if UserDefaults.standard.object(forKey: "petVisible") as? Bool ?? true { pet.show() }
        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { [weak self] _ in
            guard let self = self, self.dash != nil, self.dash.isVisible else { return }
            self.dash.setFrame(self.dashScreen().frame, display: true)
        }
    }

    // ── ядро ──
    func startServer() {
        guard FileManager.default.fileExists(atPath: SCRIPT) else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: FileManager.default.fileExists(atPath: PYTHON) ? PYTHON : "/usr/bin/python3")
        p.arguments = [SCRIPT, "--server"]
        var env = ProcessInfo.processInfo.environment
        env["PYTHONIOENCODING"] = "utf-8"
        env["LANG"] = "en_US.UTF-8"
        p.environment = env
        FileManager.default.createFile(atPath: LOG, contents: nil)
        let log = FileHandle(forWritingAtPath: LOG) ?? FileHandle.nullDevice
        p.standardOutput = log
        p.standardError = log
        do { try p.run(); server = p } catch { server = nil }
    }

    // ── дашборд на весь экран ──
    func dashScreen() -> NSScreen {
        let m = NSEvent.mouseLocation
        return NSScreen.screens.first { $0.frame.contains(m) } ?? NSScreen.main ?? NSScreen.screens.first!
    }

    func buildDashboard() {
        let f = dashScreen().frame
        dash = DashWindow(contentRect: f, styleMask: [.borderless], backing: .buffered, defer: false)
        dash.title = "JARVIS"
        dash.isOpaque = true
        dash.backgroundColor = NSColor(calibratedRed: 0.004, green: 0.02, blue: 0.043, alpha: 1)
        dash.appearance = NSAppearance(named: .darkAqua)
        dash.isReleasedWhenClosed = false
        dash.isRestorable = false
        dash.collectionBehavior = [.fullScreenAuxiliary]
        dashPage = Page(path: "/", handler: self, name: "jarvis", transparent: false)
        dashPage.web.frame = NSRect(origin: .zero, size: f.size)
        dash.contentView = dashPage.web
        dashPage.onLoad = { [weak self] in
            guard let self = self else { return }
            if !self.voiceStarted {
                self.voiceStarted = true
                self.voice.start()
            } else {
                self.voice.listen()
            }
        }
        dashPage.load()
    }

    var dashShown: Bool { dash.isVisible && dash.alphaValue > 0.5 }

    func expandDashboard() {
        guard dash != nil else { return }
        let f = dashScreen().frame
        let wasHidden = !dash.isVisible
        if wasHidden {
            dash.alphaValue = 0
            if let wf = widget.panel?.frame, widget.isShown {
                dash.setFrame(wf, display: false)                  // разворачиваемся прямо из виджета
            } else {
                dash.setFrame(f.insetBy(dx: f.width * 0.08, dy: f.height * 0.08), display: false)
            }
        }
        dash.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        NSApp.presentationOptions = [.autoHideMenuBar, .autoHideDock]
        NSAnimationContext.runAnimationGroup { ctx in
            ctx.duration = wasHidden ? 0.42 : 0.2
            ctx.timingFunction = CAMediaTimingFunction(name: .easeOut)
            dash.animator().setFrame(f, display: true)
            dash.animator().alphaValue = 1
        }
        if wasHidden { dashPage.js("window.jarvisMode && jarvisMode('full')") }
        refreshMenu()
    }

    func collapseDashboard(showWidget: Bool = true) {
        guard dash != nil, dash.isVisible else { return }
        let target = (showWidget && widget.enabled) ? (widget.panel?.frame ?? dash.frame) : dash.frame.insetBy(dx: dash.frame.width * 0.1, dy: dash.frame.height * 0.1)
        if showWidget && widget.enabled { widget.show() }
        NSAnimationContext.runAnimationGroup({ ctx in
            ctx.duration = 0.35
            ctx.timingFunction = CAMediaTimingFunction(name: .easeIn)
            dash.animator().setFrame(target, display: true)
            dash.animator().alphaValue = 0
        }, completionHandler: { [weak self] in
            guard let self = self else { return }
            self.dash.orderOut(nil)
            NSApp.presentationOptions = []
            self.refreshMenu()
        })
    }

    @objc func toggleDashboard() {
        guard dash != nil else { return }
        if dash.isVisible && dash.isKeyWindow { collapseDashboard() } else { expandDashboard() }
    }

    // ── мост в интерфейс ──
    func jsAll(_ code: String) {
        dashPage?.js(code)
        widget.page?.js(code)
    }

    func handleUI(_ ui: [String: Any]) {
        guard dash != nil else { return }
        if let d = ui["dashboard"] as? String {
            if d == "full" { expandDashboard() } else { collapseDashboard() }
        }
        if let w = ui["widget"] as? Bool {
            widget.enabled = w
            if w { widget.show(); if dash.isVisible { collapseDashboard() } } else { widget.hide() }
            refreshMenu()
        }
        if let m = ui["media"] as? String {
            DispatchQueue.global().async { mediaKey(m == "next" ? 17 : m == "prev" ? 18 : 16) }
        }
        if (ui["shutdown"] as? Bool) == true { voice.beginPower("shutdown") }
        if (ui["restart"] as? Bool) == true { voice.beginPower("restart") }
        if (ui["scan"] as? Bool) == true || (ui["voicelab"] as? Bool) == true { expandDashboard() }
        if (ui["party"] as? Bool) == true { pet.command("dance") }
        if (ui["alert"] as? Bool) == true && !dash.isVisible { expandDashboard() }
        DispatchQueue.main.asyncAfter(deadline: .now() + (dash.isVisible ? 0.05 : 0.6)) {
            self.jsAll("window.jarvisUI && window.jarvisUI(\(jsJSON(ui)))")
        }
    }

    func performAction(_ name: String) {
        switch name {
        case "shutdown", "restart": voice.beginPower(name)
        case "dashboard": expandDashboard()
        case "widget":
            if dash.isVisible { collapseDashboard() } else { widget.enabled = true; widget.show() }
        default:
            voice.runCommand(["cmd": "action", "name": name])
        }
    }

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let cmd = message.body as? String else { return }
        if cmd.hasPrefix("cmd:") {
            let t = String(cmd.dropFirst(4))
            jsAll("window.jarvisHeard && window.jarvisHeard(\(jsStr(t)))")
            voice.runCommand(["cmd": "text", "text": t])
            return
        }
        if cmd.hasPrefix("action:") { performAction(String(cmd.dropFirst(7))); return }
        if cmd.hasPrefix("say:") {
            if let d = String(cmd.dropFirst(4)).data(using: .utf8),
               let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let t = o["text"] as? String {
                voice.say(t, emotion: (o["emotion"] as? String) ?? "")
            }
            return
        }
        if cmd.hasPrefix("widget:pin:") { widget.pinned = cmd.hasSuffix("1"); refreshMenu(); return }
        switch cmd {
        case "voice:on": voice.setEnabled(true)
        case "voice:off": voice.setEnabled(false)
        case "cancel": voice.cancelPower()
        case "collapse": collapseDashboard()
        case "hide": collapseDashboard(showWidget: widget.enabled)
        case "expand": expandDashboard()
        case "widget:hide": widget.enabled = false; widget.hide(); refreshMenu()
        default: break
        }
    }

    // ── меню-бар ──
    func reactorIcon() -> NSImage {
        let img = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { r in
            NSColor.black.setStroke()
            let o = NSBezierPath(ovalIn: r.insetBy(dx: 1.5, dy: 1.5)); o.lineWidth = 1.3; o.stroke()
            let i = NSBezierPath(ovalIn: r.insetBy(dx: 5, dy: 5)); i.lineWidth = 1.5; i.stroke()
            let t = NSBezierPath()
            t.move(to: NSPoint(x: 9, y: 12.6)); t.line(to: NSPoint(x: 12.2, y: 7.2)); t.line(to: NSPoint(x: 5.8, y: 7.2)); t.close()
            t.lineWidth = 1; t.stroke()
            NSColor.black.setFill()
            NSBezierPath(ovalIn: NSRect(x: 7.7, y: 7.9, width: 2.6, height: 2.6)).fill()
            return true
        }
        img.isTemplate = true
        return img
    }

    var mWidget = NSMenuItem(), mPin = NSMenuItem(), mPet = NSMenuItem(), mPetVoice = NSMenuItem(), mMic = NSMenuItem(), mDash = NSMenuItem()

    func buildStatusItem() {
        let s = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        s.button?.image = reactorIcon()
        s.button?.toolTip = "J.A.R.V.I.S."
        let m = NSMenu()
        mDash = m.addItem(withTitle: "Развернуть дашборд", action: #selector(toggleDashboard), keyEquivalent: "j")
        mDash.keyEquivalentModifierMask = [.command, .option]
        m.addItem(NSMenuItem.separator())
        mWidget = m.addItem(withTitle: "Виджет на рабочем столе", action: #selector(toggleWidget), keyEquivalent: "")
        mPin = m.addItem(withTitle: "Виджет поверх всех окон", action: #selector(togglePin), keyEquivalent: "")
        mPet = m.addItem(withTitle: "Марк-0 на экране", action: #selector(togglePet), keyEquivalent: "")
        mPetVoice = m.addItem(withTitle: "Голос Марка-0", action: #selector(togglePetVoice), keyEquivalent: "")
        m.addItem(withTitle: "Позвать Марка к курсору", action: #selector(callPet), keyEquivalent: "")
        m.addItem(NSMenuItem.separator())
        mMic = m.addItem(withTitle: "Микрофон (слово «Джарвис»)", action: #selector(toggleMic), keyEquivalent: "")
        m.addItem(withTitle: "Тишина на 2 часа", action: #selector(quietMode), keyEquivalent: "")
        m.addItem(withTitle: "Голосовая лаборатория…", action: #selector(openLab), keyEquivalent: "")
        m.addItem(withTitle: "Полная диагностика", action: #selector(runScan), keyEquivalent: "")
        m.addItem(NSMenuItem.separator())
        m.addItem(withTitle: "Выключить мак…", action: #selector(shutdownMac), keyEquivalent: "")
        m.addItem(withTitle: "Завершить JARVIS", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        for it in m.items where it.action != #selector(NSApplication.terminate(_:)) { it.target = self }
        s.menu = m
        status = s
        refreshMenu()
    }

    func refreshMenu() {
        guard dash != nil else { return }
        mWidget.state = widget.enabled ? .on : .off
        mPin.state = widget.pinned ? .on : .off
        mPet.state = pet.isShown ? .on : .off
        mPetVoice.state = pet.voiceOn ? .on : .off
        mMic.state = voice.enabled ? .on : .off
        mDash.title = dash != nil && dash.isVisible ? "Свернуть дашборд в виджет" : "Развернуть дашборд"
    }

    @objc func toggleWidget() {
        widget.enabled.toggle()
        widget.enabled ? widget.show() : widget.hide()
        refreshMenu()
    }
    @objc func togglePin() { widget.pinned.toggle(); refreshMenu() }
    @objc func togglePet() { pet.toggle(); refreshMenu() }
    @objc func togglePetVoice() { pet.voiceOn.toggle(); refreshMenu() }
    @objc func callPet() { pet.command("come") }
    @objc func toggleMic() { voice.setEnabled(!voice.enabled) }
    @objc func quietMode() { voice.runCommand(["cmd": "text", "text": "помолчи"]) }
    @objc func openLab() { expandDashboard(); jsAll("window.jarvisUI && window.jarvisUI({voicelab:true})") }
    @objc func runScan() { voice.runCommand(["cmd": "action", "name": "scan"]) }
    @objc func shutdownMac() { voice.beginPower("shutdown") }
    @objc func reloadPage() { dashPage.load(); widget.page?.load(); pet.page?.load() }

    // ── горячая клавиша ⌥⌘J ──
    func registerHotkey() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ -> OSStatus in
            DispatchQueue.main.async { (NSApp.delegate as? AppDelegate)?.toggleDashboard() }
            return noErr
        }, 1, &spec, nil, nil)
        let id = EventHotKeyID(signature: OSType(0x4A525653), id: 1)
        RegisterEventHotKey(UInt32(kVK_ANSI_J), UInt32(cmdKey | optionKey), id, GetApplicationEventTarget(), 0, &hotKeyRef)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        expandDashboard()
        return true
    }

    func applicationDidResignActive(_ notification: Notification) {
        NSApp.presentationOptions = []
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        guard dash != nil else { return }
        if dash.isVisible && dash.isKeyWindow { NSApp.presentationOptions = [.autoHideMenuBar, .autoHideDock] }
    }

    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        if let p = server, p.isRunning { p.terminate() }
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "О JARVIS", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(NSMenuItem.separator())
        appMenu.addItem(withTitle: "Скрыть JARVIS", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Завершить JARVIS", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let editItem = NSMenuItem()
        main.addItem(editItem)
        let editMenu = NSMenu(title: "Правка")
        editMenu.addItem(withTitle: "Вырезать", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Копировать", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Вставить", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Выделить всё", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu

        let viewItem = NSMenuItem()
        main.addItem(viewItem)
        let viewMenu = NSMenu(title: "Вид")
        let t = viewMenu.addItem(withTitle: "Дашборд / виджет", action: #selector(toggleDashboard), keyEquivalent: "j")
        t.keyEquivalentModifierMask = [.command, .option]
        t.target = self
        let r = viewMenu.addItem(withTitle: "Обновить", action: #selector(reloadPage), keyEquivalent: "r")
        r.target = self
        let w = viewMenu.addItem(withTitle: "Свернуть в виджет", action: #selector(collapseFromMenu), keyEquivalent: "w")
        w.target = self
        viewItem.submenu = viewMenu

        let petItem = NSMenuItem()
        main.addItem(petItem)
        let petMenu = NSMenu(title: "Марк-0")
        let pt = petMenu.addItem(withTitle: "Показать / скрыть Марка-0", action: #selector(togglePet), keyEquivalent: "m")
        pt.keyEquivalentModifierMask = [.command, .shift]
        pt.target = self
        let pc = petMenu.addItem(withTitle: "Позвать к курсору", action: #selector(callPet), keyEquivalent: "")
        pc.target = self
        let pv = petMenu.addItem(withTitle: "Голос Марка-0", action: #selector(togglePetVoice), keyEquivalent: "")
        pv.target = self
        petItem.submenu = petMenu
        NSApp.mainMenu = main
    }

    @objc func collapseFromMenu() { collapseDashboard() }
}

if CommandLine.arguments.contains("--connect-happ") {
    exit(connectHapp())
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
