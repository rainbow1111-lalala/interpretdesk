import { useEffect, useState } from "react";

const SEEN = "mi-onboarded";

/** 首次进入弹一次。只举例，不解释界面：控件自己会说话，说明写多了没人看。 */
export function Onboarding() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!localStorage.getItem(SEEN)) setOpen(true);
  }, []);

  if (!open) return null;

  const close = () => {
    localStorage.setItem(SEEN, "1");
    setOpen(false);
  };

  return (
    <div className="onboard-bg" onClick={close}>
      <div className="onboard" onClick={(e) => e.stopPropagation()}>
        <h2>开始之前</h2>

        <p className="onboard-lead">模型设置里填你自己的 API key。</p>

        <h3>底稿</h3>
        <p>
          <span className="yes">传</span>
          问答手册、法规要点、术语对照、我方立场与底线
        </p>
        <p>
          <span className="no">别传</span>
          整本合同扫描件、几百页年报、与这场会无关的旧材料
        </p>

        <h3>会中</h3>
        <p>外放收音，设备放桌上。对方说完一段，拟稿栏自动给出可以直接念的英文。</p>
        <p>要改口就直接说，例如“回这一段，但先不接受价格调整”。</p>

        <p className="onboard-foot">
          材料上传至服务器处理，会话 7 天未使用自动删除，也可随时在会议底稿里清空。
        </p>

        <button className="onboard-ok" onClick={close}>
          知道了
        </button>
      </div>
    </div>
  );
}
