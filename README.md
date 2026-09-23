# 博阳影响培训平台

面向企业内部的在线学习平台（H5），可**嵌入企业微信自建应用**使用。按「营销 / 财务 / 人事」等分类组织视频与课件（PPT、PDF 等），员工在手机端完成学习、记录进度、参加课后测验并获得结业证书。管理员可在后台上传与管理内容。

## 功能一览
- 分类导航：营销 / 财务 / 人事（后台可增改）
- 课程与素材：视频在线播放、PPT/PDF 课件浏览器内预览与下载
- 学习进度：进入即记为「学习中」，看完/点「完成本节」记为「已完成」
- 课后测验：单选 / 多选 / 判断，自动判分、答题回顾
- 结业证书：课程素材全完成且测验合格后自动发放，可随时查看
- 学员中心：「我的」查看证书与进行中的课程
- 管理后台：分类、课程、素材上传、测验题目、学员数据
- 身份接入：企业微信网页授权自动识别员工（未配置时回退为「开发登录」）

## 快速开始（本地）
```bash
pip install -r requirements.txt
python seed.py        # 初始化三个分类与示例课程
python app.py         # 默认 http://127.0.0.1:5000
```
浏览器打开后进入「开发登录」，输入姓名即可体验（无需企业微信）。

## 嵌入企业微信（生产）
1. 企业微信后台 → 应用管理 → 自建 → 创建应用，记录 **CorpID / AgentId / Secret**。
2. 把平台部署到一个**企业微信可信域名**下（需 HTTPS，例如 https://kb.example.com），
   在主页配置填写该地址。
3. 在应用「网页授权及 JS-SDK」中配置可信域名；把证书文件放到站点根目录完成校验。
4. 启动时通过环境变量注入（不要写进代码）：
   ```bash
   set WECOM_CORP_ID=your-corpid
   set WECOM_AGENT_ID=your-agentid
   set WECOM_APP_SECRET=your-secret
   set KB_PUBLIC_BASE_URL=https://kb.example.com
   set KB_ADMIN_PASSWORD=强密码
   set KB_SECRET_KEY=随机长字符串
   ```
   配置后员工在企业微信内打开应用即用本人身份自动登录，无需单独注册。

## 目录结构
```
app.py            Flask 主程序（路由、上传、OAuth 回调）
config.py         配置（企业微信、路径、密钥，支持环境变量覆盖）
db.py             SQLite 建表与读写
wecom.py         企业微信 OAuth 身份接入
seed.py          初始化分类与示例课程
templates/       页面模板（学员端 + 管理后台）
static/css/      样式（移动端 H5）
uploads/          上传的视频/课件（运行时生成）
data/             SQLite 数据库（运行时生成）
```

## 说明
- 课件预览：视频用 HTML5 直接播放；Office 文档若本机装有 LibreOffice，上传时自动转 PDF 以便浏览器内预览，否则提供下载。
- 数据库使用 SQLite，零额外依赖；如需更高并发可替换为 MySQL（改动集中在 db.py）。
- 管理后台默认密码 `admin123`，上线务必通过 `KB_ADMIN_PASSWORD` 修改。
