from __future__ import annotations

import asyncio
import logging


async def handle_rating_popup(page):
    """监测评分弹窗, 选择五星并提交"""
    try:
        dialog = page.locator(".ant-modal-content")
        try:
            await dialog.wait_for(state="visible", timeout=1500)
            logging.info("检测到评分弹窗")
        except Exception as exc:
            logging.debug(f"未检测到评分弹窗: {exc}")
            return False

        stars_container = dialog.locator("ul.ant-rate")
        await stars_container.wait_for(state="visible", timeout=1000)

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
