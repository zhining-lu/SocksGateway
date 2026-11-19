"""生成应用图标 assets/icon.ico（蓝色渐变圆角方块 + 双向箭头环）"""
import os

from PIL import Image, ImageDraw

SIZE = 256
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
os.makedirs(OUT, exist_ok=True)


def rounded_gradient(size: int) -> Image.Image:
    base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    grad = Image.new("RGBA", (size, size))
    px = grad.load()
    c1 = (59, 123, 245)
    c2 = (29, 78, 216)
    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * size)
            px[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)) + (255,)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.24),
                                           fill=255)
    base.paste(grad, (0, 0), mask)
    return base


def main():
    img = rounded_gradient(SIZE)
    d = ImageDraw.Draw(img)
    w = int(SIZE * 0.075)
    pad = int(SIZE * 0.24)
    box = [pad, pad, SIZE - pad, SIZE - pad]
    # 两段弧，构成旋转的环
    d.arc(box, start=200, end=340, fill="white", width=w)
    d.arc(box, start=20, end=160, fill="white", width=w)
    # 箭头三角
    ax, ay = int(SIZE * 0.70), int(SIZE * 0.20)
    s = int(SIZE * 0.115)
    d.polygon([(ax, ay - s), (ax + s, ay + int(s * 0.55)), (ax - s, ay + int(s * 0.55))],
              fill="white")
    ax2, ay2 = int(SIZE * 0.30), int(SIZE * 0.80)
    d.polygon([(ax2, ay2 + s), (ax2 + s, ay2 - int(s * 0.55)), (ax2 - s, ay2 - int(s * 0.55))],
              fill="white")
    png = os.path.join(OUT, "icon.png")
    img.save(png)
    img.save(os.path.join(OUT, "icon.ico"),
             sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("saved", png)


if __name__ == "__main__":
    main()
