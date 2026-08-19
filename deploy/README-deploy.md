# 部署到服务器

服务器：Ubuntu，已放行 22 / 80 / 443。全程 root 密码只在你自己机器上输入，不进任何对话。

## 一、在你的 Mac 上打包

```bash
cd ~/AIwork/03-AI项目/meeting-interpreter
bash deploy/pack.sh
```

打出 `/tmp/interpretdesk-deploy.tar.gz`。**包里不含 `data/`**，也就是不含任何会议底稿、
API key 与会议记录，只有代码和构建好的前端。上传前脚本会把包内清单打出来，自己看一眼。

## 二、上传并安装

```bash
scp /tmp/interpretdesk-deploy.tar.gz root@<服务器IP>:/root/
ssh root@<服务器IP>
```

登上服务器后：

```bash
mkdir -p /opt/interpretdesk
tar -xzf /root/interpretdesk-deploy.tar.gz -C /opt/interpretdesk
bash /opt/interpretdesk/deploy/install.sh
```

最后一行会打印本机自测的 HTTP 状态码，200 就是装好了。

## 三、域名解析（在 Cloudflare 做，改动前先确认）

在 Cloudflare 的 interpretdesk.com 里加两条 A 记录，都指向服务器 IP：

| 类型 | 名称 | 内容 | 代理状态 |
|---|---|---|---|
| A | `@` | 服务器 IP | 先关（DNS only，灰云） |
| A | `www` | 服务器 IP | 先关（DNS only，灰云） |

**先关代理**，因为下一步签证书要让 Let's Encrypt 直接访问到服务器。证书签好后再决定要不要开。

## 四、签 HTTPS 证书

解析生效后（`dig interpretdesk.com +short` 能返回服务器 IP），在服务器上：

```bash
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d interpretdesk.com -d www.interpretdesk.com --agree-tos -m <你的邮箱> --redirect
```

certbot 会自己改 nginx 配置并装上自动续期。完成后浏览器打开 https://interpretdesk.com 。

签好后如果要开 Cloudflare 代理（橙云），必须同时把 SSL/TLS 模式设成 **Full (strict)**。
设成 Flexible 会让 Cloudflare 到服务器这一段走明文，会话 cookie 和用户上传的材料都在里面。

## 五、上线后自查

- 打开站点，右上角模型设置里填自己的 API key，上传一份测试文件，确认能提炼。
- 换一个浏览器（或无痕窗口）打开，确认看不到上一个浏览器的底稿。这是多人版的底线。
- `systemctl status interpretdesk` 看服务是否常驻，`journalctl -u interpretdesk -f` 看实时日志。

## 常用运维

```bash
systemctl restart interpretdesk     # 重启
journalctl -u interpretdesk -n 100  # 看最近日志
du -sh /opt/interpretdesk/data/sessions/*   # 看各会话占了多少磁盘
```

## 更新代码

在 Mac 上重新 `bash deploy/pack.sh`，上传后：

```bash
tar -xzf /root/interpretdesk-deploy.tar.gz -C /opt/interpretdesk
chown -R interpretdesk:interpretdesk /opt/interpretdesk
systemctl restart interpretdesk
```

`data/` 不在包里，更新不会动用户的材料。
