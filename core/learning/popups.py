from __future__ import annotations

import asyncio
import logging


async def handle_archive_continue_popup(page) -> bool:
    """归档课确认：「该课程已归档，是否继续学习？」→ 点「继续学习」。

    实勘有两种 UI：
    1) 中转页 ``.study-transition-page``（常见）：``div.btn``「继续学习」，
       点取消则无法进入课程；须在章节 wait 前处理。
    2) ant-modal 弹窗（兼容）：``.ant-modal-content`` 内按钮「继续学习」。
    """
    try:
        # --- 路径 1：学习中转页（transition-page）---
        transition = page.locator(".study-transition-page").filter(has_text="归档")
        try:
            await transition.first.wait_for(state="visible", timeout=2000)
        except Exception:
            transition = None

        if transition is not None and await transition.count() > 0:
            # 实勘：<div id="*goOnStudy" class="btn">继续学习</div>（非 <button>）
            continue_btn = transition.locator("div.btn").filter(has_text="继续学习")
            if await continue_btn.count() == 0:
                continue_btn = page.locator("[id$='goOnStudy']")
            if await continue_btn.count() == 0:
                continue_btn = transition.get_by_text("继续学习", exact=True)
            if await continue_btn.count() == 0:
                logging.warning("检测到归档中转页，但未找到「继续学习」")
                return False

            await continue_btn.first.click()
            logging.info("检测到归档课程中转页，已点击继续学习")
            try:
                await transition.first.wait_for(state="hidden", timeout=5000)
            except Exception:
                await page.wait_for_timeout(800)
            try:
                await page.wait_for_load_state("load", timeout=10000)
            except Exception:
                pass
            return True

        # --- 路径 2：ant-modal（兼容）---
        dialog = page.locator(".ant-modal-content").filter(has_text="归档")
        try:
            await dialog.first.wait_for(state="visible", timeout=1500)
        except Exception:
            return False

        continue_btn = dialog.locator("button").filter(has_text="继续学习")
        if await continue_btn.count() == 0:
            continue_btn = dialog.locator("div.btn, a, span").filter(has_text="继续学习")
        if await continue_btn.count() == 0:
            continue_btn = page.get_by_role("button", name="继续学习")
        if await continue_btn.count() == 0:
            logging.warning("检测到归档确认弹窗，但未找到「继续学习」按钮")
            return False

        await continue_btn.first.click()
        logging.info("检测到归档课程确认弹窗，已点击继续学习")
        try:
            await dialog.first.wait_for(state="hidden", timeout=3000)
        except Exception:
            await page.wait_for_timeout(500)
        return True
    except Exception as exc:
        logging.warning(f"处理归档确认弹窗时出错: {exc}")
        return False


async def handle_rating_popup(page):
    """监测评分弹窗, 选择五星并提交。

    无弹窗是常态：用 count/短探测，避免每次 DEBUG 刷 Playwright Timeout + Call log。
    """
    try:
        dialog = page.locator(".ant-modal-content")
        # 先廉价探测：多数章节没有评分弹窗
        try:
            if await dialog.count() == 0:
                return False
            if not await dialog.first.is_visible():
                return False
        except Exception:
            return False

        try:
            await dialog.first.wait_for(state="visible", timeout=800)
        except Exception:
            return False

        logging.info("检测到评分弹窗")

        # 归档确认也是 ant-modal，勿当评分弹窗硬点「确定」
        try:
            dialog_text = await dialog.first.inner_text(timeout=500)
        except Exception:
            dialog_text = ""
        if "归档" in (dialog_text or ""):
            return await handle_archive_continue_popup(page)

        stars_container = dialog.locator("ul.ant-rate")
        try:
            await stars_container.wait_for(state="visible", timeout=1000)
        except Exception:
            # 有 ant-modal 但不是评分（其它业务弹窗）
            return False

        try:
            fifth_star = dialog.locator("ul.ant-rate li:nth-child(5) div[role='radio']")
            await fifth_star.wait_for(state="visible", timeout=1000)
            await page.evaluate(
                "document.querySelector('ul.ant-rate').scrollIntoView({block: 'center'})"
            )
            await fifth_star.click(force=True)
            logging.info("已五星评价")
        except Exception as exc:
            logging.warning(f"点击星星失败: {exc}")

        await page.wait_for_timeout(500)

        try:
            confirm_button = page.get_by_role("button", name="确 定")
            await confirm_button.click()
            logging.info("已点击确定按钮")
            return True
        except Exception as exc:
            logging.error(f"点击确定按钮时出错: {exc}")
            return False
    except Exception as exc:
        logging.error(f"处理评分弹窗时出错: {exc}")
        return False


async def check_and_handle_rating_popup(page):
    """检查并处理视频内课程质量评价弹窗"""
    try:
        popup_exists = (
            await page.locator(
                "div.split-section-detail-header--interact:has-text('互动练习')"
            ).count()
            > 0
        )

        if popup_exists:
            logging.info("检测到课程质量评价弹窗")
            skip_button = page.locator("button:has-text('跳 过')")
            if await skip_button.count() > 0:
                await skip_button.click()
                logging.info("已点击'跳过'按钮")
                await page.wait_for_timeout(1000)
                return True
    except Exception as exc:
        logging.warning(f"处理评价弹窗时出错: {str(exc)}")

    return False


async def check_rating_popup_periodically(page, duration, interval=30):
    """定期检查视频内评价弹窗, 持续指定时间"""
    elapsed = 0
    while elapsed < duration:
        wait_time = min(interval, duration - elapsed)
        await asyncio.sleep(wait_time)
        await check_and_handle_rating_popup(page)
        elapsed += wait_time
