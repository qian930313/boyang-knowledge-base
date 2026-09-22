"""
种子数据：初始化 营销 / 财务 / 人事 三个分类，并为每个分类创建一门示例课程。
运行：python seed.py
真实的中文课件(PPT/PDF/视频)请在管理后台上传，会正确显示中文。
"""
import config
import db


def seed():
    db.init_db()
    cats = [
        ("marketing", "营销", "📣"),
        ("finance", "财务", "💰"),
        ("hr", "人事", "👥"),
    ]
    for key, name, icon in cats:
        exist = db.fetch_one("SELECT id FROM categories WHERE key=?", (key,))
        if exist:
            print("分类已存在，跳过：%s" % name)
            continue
        cid = db.execute("INSERT INTO categories(key,name,icon,sort) VALUES(?,?,?,?)",
                         (key, name, icon, 0))
        # 示例课程（不含任何演示素材，课件请在后台上传）
        db.execute(
            "INSERT INTO courses(category_id,title,desc,required,sort,created_at) VALUES(?,?,?,?,?,?)",
            (cid, "%s入门课程" % name, "示例课程，管理员可在后台替换为真实内容并上传课件。", 1, 0, db.now()),
        )
        print("已创建分类[%s]及示例课程" % name)
    print("种子数据完成。")


if __name__ == "__main__":
    seed()
