import AppKit

// 사용법: swift make_icon.swift <출력.png>  — 1024px 앱 아이콘 (터미널 프롬프트 + 잔디)
func rgb(_ hex: Int) -> NSColor {
    NSColor(srgbRed: CGFloat(hex >> 16 & 255) / 255, green: CGFloat(hex >> 8 & 255) / 255, blue: CGFloat(hex & 255) / 255, alpha: 1)
}
let px = 1024
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8, samplesPerPixel: 4,
                           hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

// macOS 아이콘 격자: 1024 캔버스 안 824 크기의 둥근 사각형
let card = NSBezierPath(roundedRect: NSRect(x: 100, y: 100, width: 824, height: 824), xRadius: 185, yRadius: 185)
rgb(0x0d1117).setFill()
card.fill()
rgb(0x30363d).setStroke()
card.lineWidth = 8
card.stroke()

// 프롬프트 ">" 와 블록 커서
let prompt = NSAttributedString(string: ">", attributes: [
    .font: NSFont.monospacedSystemFont(ofSize: 300, weight: .bold), .foregroundColor: rgb(0x56d364)])
prompt.draw(at: NSPoint(x: 180, y: 500))
rgb(0x56d364).setFill()
NSBezierPath(rect: NSRect(x: 390, y: 555, width: 120, height: 36)).fill()

// 잔디 격자 (앱의 초록 5단계)
let heat = [0x161b22, 0x0e4429, 0x006d32, 0x26a641, 0x39d353].map(rgb)
let levels = [[0, 2, 1, 3, 2, 4], [1, 0, 3, 2, 4, 3], [0, 1, 0, 4, 3, 4]]
for (r, row) in levels.enumerated() {
    for (c, v) in row.enumerated() {
        heat[v].setFill()
        NSBezierPath(roundedRect: NSRect(x: 190 + c * 110, y: 380 - r * 110, width: 92, height: 92), xRadius: 14, yRadius: 14).fill()
    }
}

NSGraphicsContext.current = nil
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
