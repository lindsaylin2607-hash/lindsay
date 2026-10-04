import streamlit as st
import pandas as pd
import re
import io
import os
import json
import time
from datetime import datetime, date
from pathlib import Path
from google import genai
from google.genai import types

st.set_page_config(
    page_title="冷媒設備智慧管理與碳盤查系統",
    page_icon="🧊",
    layout="wide"
)

# 建立銘牌佐證庫資料夾
EVIDENCE_DIR = Path("evidence_nameplates")
EVIDENCE_DIR.mkdir(exist_ok=True)

# ----------------- 側邊欄：盤查年度、API 與 GWP 因子庫 -----------------
with st.sidebar:
    st.header("⚙️ 盤查參數設定")
    inventory_year = st.number_input("📅 當前盤查/評估基準年", min_value=2020, max_value=2035, value=2026, step=1)
    gemini_api_key = st.text_input("Google Gemini API Key", type="password", help="可由 aistudio.google.com 免費取得")
    
    st.markdown("---")
    gwp_version = st.selectbox("選擇 GWP 評估版本", ["IPCC AR4 (預設)", "IPCC AR5", "IPCC AR6"])
    GWP_TABLES = {
        "IPCC AR4 (預設)": {
            "R-410A": 2088, "R-134A": 1430, "R-32": 675, "R-404A": 3922, 
            "R-407C": 1774, "R-22": 1810, "R-507A": 3985, "R-12": 10900, "R-600A": 3, "R-417A": 2346
        },
        "IPCC AR5": {
            "R-410A": 1924, "R-134A": 1300, "R-32": 677, "R-404A": 3943, 
            "R-407C": 1624, "R-22": 1760, "R-507A": 3985, "R-12": 10200, "R-600A": 3, "R-417A": 2127
        },
        "IPCC AR6": {
            "R-410A": 2256, "R-134A": 1530, "R-32": 771, "R-404A": 4728, 
            "R-407C": 1908, "R-22": 1960, "R-507A": 4820, "R-12": 11200, "R-600A": 0.06, "R-417A": 2439
        }
    }
    current_gwp_dict = GWP_TABLES[gwp_version]

# ----------------- 輔助正規化、月數加權與編碼函式 -----------------
def clean_ref_name(ref_str: str) -> str:
    if not ref_str or pd.isna(ref_str):
        return "R-410A"
    clean = str(ref_str).strip().upper().replace(" ", "")
    m = re.search(r'R[-_]?(\d+[A-Z]*)', clean)
    if m:
        return f"R-{m.group(1)}"
    return clean

def parse_location(location_str: str) -> str:
    loc = str(location_str).strip()
    floor_match = re.search(r'([A-Za-z0-9]+[FfBb][0-9]*)', loc)
    if floor_match:
        return floor_match.group(1).upper()
    line_match = re.search(r'(?:線|Line|line)([0-9]+)|([0-9]+)線', loc)
    if line_match:
        return f"L{line_match.group(1) or line_match.group(2)}"
    chinese_floors = {"一樓": "1F", "二樓": "2F", "三樓": "3F", "四樓": "4F", "五樓": "5F"}
    for k, v in chinese_floors.items():
        if k in loc: return v
    return "GF"

def parse_site_code(dept_str: str) -> str:
    dept = str(dept_str).upper()
    if "BIKE" in dept: return "BIKE"
    elif "TM" in dept: return "TM"
    elif "HQ" in dept or "總部" in dept: return "HQ"
    elif "焊接" in dept: return "WELD"
    elif "生管" in dept: return "PC"
    clean = re.sub(r'[^A-Za-z0-9]', '', dept)
    return clean[:4] if clean else "MAIN"

def get_type_code(type_str: str) -> str:
    mapping = {
        "商業/辦公室空調": "COM", "住家及商業建築冷氣": "COM", "大型冰水主機": "CHILL",
        "冰箱/冷凍冷藏設備": "RF", "製程冷凍乾燥設備": "DRY", "飲水機": "WD", "公務車輛空調": "CAR"
    }
    for k, v in mapping.items():
        if k in str(type_str): return v
    return "AC"

def generate_asset_id(site: str, eq_type: str, loc: str, seq_num: int) -> str:
    s_code = parse_site_code(site)
    t_code = get_type_code(eq_type)
    floor = parse_location(loc)
    return f"REF-{s_code}-{t_code}-{floor}-{seq_num:04d}"

def calculate_time_weighted_emissions(charge_kg: float, gwp: float, install_date_str: str, scrap_date_str: str, inv_year: int):
    """
    依 ISO 14064-1 計算盤查年度實際有效營運月數加權碳排放當量 (tCO2e)
    """
    total_potential = (charge_kg * gwp) / 1000.0
    
    # 解析日期
    def parse_dt(d_str):
        if not d_str or pd.isna(d_str) or str(d_str).strip() in ["", "nan", "None"]:
            return None
        try:
            return pd.to_datetime(d_str).date()
        except:
            return None

    inst_d = parse_dt(install_date_str)
    scrap_d = parse_dt(scrap_date_str)
    
    # 判斷報廢年度
    if scrap_d and scrap_d.year < inv_year:
        return 0.0, 0, "前年度已報廢(不計入當期)"

    # 計算當年度有效營運區間 (1~12月)
    start_month = 1
    if inst_d and inst_d.year == inv_year:
        start_month = inst_d.month
    elif inst_d and inst_d.year > inv_year:
        return 0.0, 0, "未來年度設置"
        
    end_month = 12
    if scrap_d and scrap_d.year == inv_year:
        end_month = scrap_d.month
        
    active_months = max(0, end_month - start_month + 1)
    weighted_tco2e = round(total_potential * (active_months / 12.0), 4)
    desc = f"運轉 {active_months}/12 個月" if active_months < 12 else "全年度運轉 (12/12月)"
    return weighted_tco2e, active_months, desc

# ----------------- 全域 Session State -----------------
STANDARD_COLUMNS = [
    "設備編號 (Asset ID)", "廠區/據點代碼", "使用/保管單位", "具體裝設地點",
    "設備類別", "廠牌", "機型編號", "冷媒種類", "額定充填量 (kg/台)", "數量 (台)",
    "設備總充填量 (kg)", "對應 GWP 值", "設置日期", "報廢日期", "當年度有效月數",
    "潛在排放總量 (tCO2e)", "盤查計算路徑", "設備狀態", "原廠銘牌佐證"
]

if "refrigerant_inventory" not in st.session_state:
    st.session_state.refrigerant_inventory = pd.DataFrame(columns=STANDARD_COLUMNS)

if "scrapped_inventory" not in st.session_state:
    st.session_state.scrapped_inventory = pd.DataFrame(columns=STANDARD_COLUMNS)

# ----------------- 主介面佈局 -----------------
st.title("🧊 冷媒設備智慧管理與碳盤查系統")

tab_import, tab_ocr, tab_change, tab_manage, tab_scrap = st.tabs([
    "📥 1. 批次匯入清單",
    "📸 2. 銘牌 AI 智慧辨識",
    "🔄 3. 冷媒設備異動 (增/刪/修/報廢)",
    "📋 4. 在役台帳總覽與編修",
    "📦 5. 報廢/除役設備清冊"
])

# ==================== Tab 1: 批次匯入清單 ====================
with tab_import:
    st.subheader("批次匯入既有設備清冊 (.xlsx 或 .csv)")
    st.markdown("自動比對欄位名稱、去除合計列、匹配 GWP，並**自動辨識設置/報廢日期**分流在役與報廢設備。")
    
    import_file = st.file_uploader("請選擇清冊檔案", type=["xlsx", "csv"], key="batch_import")
    
    if import_file:
        try:
            if import_file.name.endswith(".xlsx"):
                xls = pd.ExcelFile(import_file)
                sheet = st.selectbox("選擇工作表 (Sheet)", xls.sheet_names)
                df_raw = pd.read_excel(import_file, sheet_name=sheet)
            else:
                df_raw = pd.read_csv(import_file)
            
            col_map = {c: re.sub(r'[\r\n\t]+', ' ', str(c)).strip() for c in df_raw.columns}
            df_clean = df_raw.rename(columns=col_map)
            
            first_col = df_clean.columns[0]
            df_clean = df_clean[~df_clean[first_col].astype(str).str.contains(r'合計|Total|Sum', case=False, na=False)]
            
            def find_col(candidates, cols):
                for c in candidates:
                    for col in cols:
                        if c in col: return col
                return None
            
            cols = df_clean.columns.tolist()
            c_id = find_col(["設備編號", "Asset ID", "設備代碼", "財產編號"], cols)
            c_site = find_col(["廠區/據點代碼", "廠區", "據點"], cols)
            c_dept = find_col(["使用/保管單位", "保管單位", "部門"], cols)
            c_loc = find_col(["具體裝設地點", "裝設地點", "位置", "放置地點"], cols)
            c_type = find_col(["設備類別", "設備類型/用途", "細項類型", "品項"], cols)
            c_brand = find_col(["廠牌"], cols)
            c_model = find_col(["機型編號", "型號"], cols)
            c_ref = find_col(["冷媒種類", "清冊冷媒", "冷媒"], cols)
            c_charge = find_col(["額定充填量", "填充量", "充填量"], cols)
            c_path = find_col(["盤查計算路徑", "計算路徑"], cols)
            c_status = find_col(["設備狀態", "狀態"], cols)
            c_doc = find_col(["原廠銘牌佐證", "佐證", "備註"], cols)
            c_inst = find_col(["設置日期", "啟用安裝年份", "取得日期", "安裝年份"], cols)
            c_scrap = find_col(["報廢日期", "除役日期", "報廢年份"], cols)
            
            if st.button("🚀 確認匯入並自動分流在役與報廢清冊", type="primary"):
                active_rows = []
                scrapped_rows = []
                current_len = len(st.session_state.refrigerant_inventory) + len(st.session_state.scrapped_inventory)
                
                for idx, r in df_clean.iterrows():
                    site_val = str(r[c_site]).strip() if c_site and pd.notna(r[c_site]) else "JHT1-BIKE廠"
                    dept_val = str(r[c_dept]).strip() if c_dept and pd.notna(r[c_dept]) else "Bike線"
                    loc_val = str(r[c_loc]).strip() if c_loc and pd.notna(r[c_loc]) else "1F"
                    type_val = str(r[c_type]).strip() if c_type and pd.notna(r[c_type]) else "商業/辦公室空調"
                    brand_val = str(r[c_brand]).strip() if c_brand and pd.notna(r[c_brand]) else "未知"
                    model_val = str(r[c_model]).strip() if c_model and pd.notna(r[c_model]) else "未知"
                    status_val = str(r[c_status]).strip() if c_status and pd.notna(r[c_status]) else "運轉中"
                    path_val = str(r[c_path]).strip() if c_path and pd.notna(r[c_path]) else "設備逸散率推估法"
                    doc_val = str(r[c_doc]).strip() if c_doc and pd.notna(r[c_doc]) and str(r[c_doc]).strip() != "nan" else "尚未上傳"
                    inst_val = str(r[c_inst]).strip() if c_inst and pd.notna(r[c_inst]) and str(r[c_inst]).strip() != "nan" else "2020-01-01"
                    scrap_val = str(r[c_scrap]).strip() if c_scrap and pd.notna(r[c_scrap]) and str(r[c_scrap]).strip() != "nan" else ""
                    
                    try:
                        rate_val = float(re.findall(r"[-+]?(?:\d*\.\d+|\d+)", str(r[c_charge]))[0]) if c_charge and pd.notna(r[c_charge]) else 0.0
                    except:
                        rate_val = 0.0
                    
                    qty_val = 1
                    total_charge = round(rate_val * qty_val, 3)
                    raw_ref = str(r[c_ref]).strip() if c_ref and pd.notna(r[c_ref]) else "R-410A"
                    norm_ref = clean_ref_name(raw_ref)
                    gwp_val = current_gwp_dict.get(norm_ref, 0)
                    
                    # 計算月數加權碳排
                    tco2e_val, act_months, desc = calculate_time_weighted_emissions(total_charge, gwp_val, inst_val, scrap_val, inventory_year)
                    
                    if c_id and pd.notna(r[c_id]) and str(r[c_id]).strip() != "":
                        asset_id = str(r[c_id]).strip()
                    else:
                        asset_id = generate_asset_id(site_val, type_val, loc_val, current_len + idx + 1)
                        
                    row_data = {
                        "設備編號 (Asset ID)": asset_id,
                        "廠區/據點代碼": site_val,
                        "使用/保管單位": dept_val,
                        "具體裝設地點": loc_val,
                        "設備類別": type_val,
                        "廠牌": brand_val,
                        "機型編號": model_val,
                        "冷媒種類": norm_ref,
                        "額定充填量 (kg/台)": rate_val,
                        "數量 (台)": qty_val,
                        "設備總充填量 (kg)": total_charge,
                        "對應 GWP 值": gwp_val,
                        "設置日期": inst_val,
                        "報廢日期": scrap_val,
                        "當年度有效月數": act_months,
                        "潛在排放總量 (tCO2e)": tco2e_val,
                        "盤查計算路徑": path_val,
                        "設備狀態": status_val,
                        "原廠銘牌佐證": doc_val
                    }
                    
                    # 自動分流邏輯：報廢日期早於盤查年度者自動歸入報廢清單
                    is_past_scrap = False
                    if scrap_val:
                        try:
                            s_year = pd.to_datetime(scrap_val).year
                            if s_year < inventory_year:
                                is_past_scrap = True
                        except:
                            pass
                            
                    if is_past_scrap or "報廢" in status_val or "除役" in status_val:
                        row_data["設備狀態"] = "已報廢除役"
                        scrapped_rows.append(row_data)
                    else:
                        active_rows.append(row_data)
                    
                if active_rows:
                    st.session_state.refrigerant_inventory = pd.concat([
                        st.session_state.refrigerant_inventory, pd.DataFrame(active_rows)
                    ], ignore_index=True)
                if scrapped_rows:
                    st.session_state.scrapped_inventory = pd.concat([
                        st.session_state.scrapped_inventory, pd.DataFrame(scrapped_rows)
                    ], ignore_index=True)
                
                st.success(f"🎉 匯入完成！在役設備共 {len(active_rows)} 筆，歷史報廢封存 {len(scrapped_rows)} 筆（已自動分流至第 5 分頁）。")
        except Exception as err:
            st.error(f"檔案解析失敗：{err}")

# ==================== Tab 2: 銘牌 AI 智慧辨識 ====================
with tab_ocr:
    st.subheader("📸 銘牌相片 / PDF 智慧辨識登錄")
    ocr_file = st.file_uploader("上傳銘牌照片或 PDF 規格書", type=["jpg", "jpeg", "png", "pdf"], key="ocr_uploader")
    
    col_ai1, col_ai2 = st.columns(2)
    with col_ai1:
        ai_site = st.text_input("廠區/據點代碼", value="JHT1-BIKE廠", key="ai_site")
        ai_dept = st.text_input("使用/保管單位", value="Bike線", key="ai_dept")
        ai_loc = st.text_input("具體裝設地點", value="1F辦公室", key="ai_loc")
        ai_type = st.selectbox("設備類別", ["商業/辦公室空調", "大型冰水主機", "冰箱/冷凍冷藏設備", "飲水機", "製程冷凍乾燥設備", "公務車輛空調"], key="ai_type")
        ai_inst_date = st.date_input("設置啟用日期", value=date.today(), key="ai_inst_date")
            
    with col_ai2:
        if ocr_file and ocr_file.type.startswith("image"):
            st.image(ocr_file, caption="銘牌預覽", use_container_width=True)

    if ocr_file and st.button("🚀 開始 AI 辨識並自動登錄", type="primary"):
        if not gemini_api_key:
            st.error("請先在左側邊欄輸入 Google Gemini API Key！")
        else:
            client = genai.Client(api_key=gemini_api_key)
            file_bytes = ocr_file.read()
            mime_type = ocr_file.type
            ext = ocr_file.name.split(".")[-1]
            
            prompt = """
            你是一位專業的冷凍空調與碳盤查工程師。請精確分析此設備銘牌或規格文件，提取以下欄位並以純 JSON 格式輸出：
            {
                "brand": "廠牌名稱(如日立、大金、聲寶、Panasonic、沛宸，若無填未知)",
                "model": "機型編號(Model Name)",
                "refrigerant": "冷媒種類(如 R-410A, R-32, R-134a, R-22, R-600a)",
                "charge_kg": 冷媒充填量數值(浮點數，單位強制為 kg)
            }
            注意：若數值單位為 g 或公克，請除以 1000 換算為 kg。只回傳 JSON 物件，不要任何額外說明。
            """
            
            response = None
            for attempt in range(3):
                try:
                    with st.spinner(f"Gemini 正在辨識銘牌參數 (第 {attempt + 1} 次嘗試)..."):
                        response = client.models.generate_content(
                            model='gemini-3.8-flash',
                            contents=[prompt, types.Part.from_bytes(data=file_bytes, mime_type=mime_type)]
                        )
                        break
                except Exception as ex:
                    if "503" in str(ex) and attempt < 2:
                        time.sleep(2)
                        continue
                    else:
                        st.error(f"辨識失敗：{ex}")
                        break
                        
            if response:
                try:
                    raw_text = response.text.strip()
                    json_match = re.search(r'\{.*\}', raw_text, re.DOTALL)
                    clean_json = json_match.group(0) if json_match else raw_text
                    parsed = json.loads(clean_json)
                    
                    rate_kg = float(parsed.get("charge_kg", 0.0))
                    ref_name = clean_ref_name(str(parsed.get("refrigerant", "R-410A")))
                    gwp_val = current_gwp_dict.get(ref_name, 0)
                    
                    tco2e_val, act_months, desc = calculate_time_weighted_emissions(rate_kg, gwp_val, str(ai_inst_date), "", inventory_year)
                    new_asset_id = generate_asset_id(ai_site, ai_type, ai_loc, len(st.session_state.refrigerant_inventory) + 1)
                    saved_filename = f"{new_asset_id}_銘牌.{ext}"
                    save_path = EVIDENCE_DIR / saved_filename
                    with open(save_path, "wb") as f:
                        f.write(file_bytes)
                        
                    new_row = {
                        "設備編號 (Asset ID)": new_asset_id,
                        "廠區/據點代碼": ai_site,
                        "使用/保管單位": ai_dept,
                        "具體裝設地點": ai_loc,
                        "設備類別": ai_type,
                        "廠牌": parsed.get("brand", "未知"),
                        "機型編號": parsed.get("model", "未知"),
                        "冷媒種類": ref_name,
                        "額定充填量 (kg/台)": rate_kg,
                        "數量 (台)": 1,
                        "設備總充填量 (kg)": rate_kg,
                        "對應 GWP 值": gwp_val,
                        "設置日期": str(ai_inst_date),
                        "報廢日期": "",
                        "當年度有效月數": act_months,
                        "潛在排放總量 (tCO2e)": tco2e_val,
                        "盤查計算路徑": "設備逸散率推估法",
                        "設備狀態": "運轉中",
                        "原廠銘牌佐證": saved_filename
                    }
                    st.session_state.refrigerant_inventory = pd.concat([
                        st.session_state.refrigerant_inventory, pd.DataFrame([new_row])
                    ], ignore_index=True)
                    st.success(f"🎉 成功新增設備【{new_asset_id}】！銘牌已自動歸檔為 `{saved_filename}`！({desc})")
                    st.json(parsed)
                except Exception as parse_err:
                    st.error(f"處理失敗：{parse_err}")

# ==================== Tab 3: 冷媒設備異動 (增/刪/修/報廢) ====================
with tab_change:
    st.subheader("🔄 冷媒設備異動管理中心")
    st.markdown("在此進行**新增進場設備**、**現有設備修改/除役登記**，或**誤登資料永久刪除**。報廢設備將按盤查年度實際有效月數精確加權碳排！")
    
    change_mode = st.radio("請選擇異動作業類型：", ["➕ 新增進場設備", "📝 變更設備狀態 / 登記報廢", "🗑️ 刪除誤登設備"], horizontal=True)
    
    # 模式 A: 新增設備
    if change_mode == "➕ 新增進場設備":
        with st.form("add_equipment_form", clear_on_submit=True):
            st.markdown("##### 填寫新設備資料")
            c1, c2, c3 = st.columns(3)
            with c1:
                n_site = st.text_input("廠區/據點代碼 *", value="JHT1-BIKE廠")
                n_dept = st.text_input("使用/保管單位 *", value="Bike線")
                n_loc = st.text_input("具體裝設地點 *", placeholder="例：1F辦公室")
            with c2:
                n_type = st.selectbox("設備類別", ["商業/辦公室空調", "大型冰水主機", "冰箱/冷凍冷藏設備", "飲水機", "製程冷凍乾燥設備", "公務車輛空調"])
                n_brand = st.text_input("廠牌", placeholder="例：日立")
                n_model = st.text_input("機型編號", placeholder="例：RAC-71QD")
            with c3:
                n_ref = st.selectbox("冷媒種類", list(current_gwp_dict.keys()))
                n_rate = st.number_input("額定充填量 (kg/台) *", min_value=0.0, value=1.60, step=0.1)
                n_inst_date = st.date_input("設置啟用日期 *", value=date.today())
                
            submitted_add = st.form_submit_button("➕ 確認新增並加入台帳", type="primary")
            if submitted_add:
                if not n_site or not n_loc:
                    st.warning("請填寫必填欄位（廠區代碼與裝設地點）！")
                else:
                    gwp_val = current_gwp_dict.get(n_ref, 0)
                    tco2e_val, act_months, desc = calculate_time_weighted_emissions(n_rate, gwp_val, str(n_inst_date), "", inventory_year)
                    new_id = generate_asset_id(n_site, n_type, n_loc, len(st.session_state.refrigerant_inventory) + 1)
                    
                    entry = {
                        "設備編號 (Asset ID)": new_id,
                        "廠區/據點代碼": n_site,
                        "使用/保管單位": n_dept,
                        "具體裝設地點": n_loc,
                        "設備類別": n_type,
                        "廠牌": n_brand,
                        "機型編號": n_model,
                        "冷媒種類": n_ref,
                        "額定充填量 (kg/台)": n_rate,
                        "數量 (台)": 1,
                        "設備總充填量 (kg)": n_rate,
                        "對應 GWP 值": gwp_val,
                        "設置日期": str(n_inst_date),
                        "報廢日期": "",
                        "當年度有效月數": act_months,
                        "潛在排放總量 (tCO2e)": tco2e_val,
                        "盤查計算路徑": "設備逸散率推估法",
                        "設備狀態": "運轉中",
                        "原廠銘牌佐證": "尚未上傳"
                    }
                    st.session_state.refrigerant_inventory = pd.concat([
                        st.session_state.refrigerant_inventory, pd.DataFrame([entry])
                    ], ignore_index=True)
                    st.success(f"🎉 成功新增設備【{new_id}】！{desc}，當期潛在排放：{tco2e_val} tCO2e")
                    
    # 模式 B: 修改 / 登記報廢
    elif change_mode == "📝 變更設備狀態 / 登記報廢":
        df_active = st.session_state.refrigerant_inventory
        if df_active.empty:
            st.info("目前無在役設備可供異動。")
        else:
            target_id = st.selectbox("請選擇要異動/報廢的設備編號：", df_active["設備編號 (Asset ID)"].unique())
            row_idx = df_active[df_active["設備編號 (Asset ID)"] == target_id].index[0]
            curr_row = df_active.loc[row_idx]
            
            with st.form("update_equipment_form"):
                st.markdown(f"##### 異動設備：`{target_id}`")
                u1, u2, u3 = st.columns(3)
                with u1:
                    up_status = st.selectbox("變更設備狀態", ["運轉中", "停用備用", "已報廢除役"], index=0 if curr_row["設備狀態"]=="運轉中" else 2)
                    up_loc = st.text_input("具體裝設地點", value=curr_row["具體裝設地點"])
                with u2:
                    up_charge = st.number_input("額定充填量 (kg)", min_value=0.0, value=float(curr_row["額定充填量 (kg/台)"]), step=0.1)
                    up_inst = st.text_input("設置日期 (YYYY-MM-DD)", value=str(curr_row["設置日期"]))
                with u3:
                    is_scrap = st.checkbox("設備是否已報廢除役？", value=(up_status == "已報廢除役"))
                    default_scrap_d = date.today() if is_scrap else None
                    up_scrap_date = st.date_input("報廢除役日期", value=default_scrap_d if is_scrap else date.today(), disabled=not is_scrap)
                    
                submitted_up = st.form_submit_button("💾 確認送出異動並重新核算", type="primary")
                if submitted_up:
                    ref_norm = clean_ref_name(curr_row["冷媒種類"])
                    gwp_val = current_gwp_dict.get(ref_norm, 0)
                    scrap_str = str(up_scrap_date) if is_scrap else ""
                    
                    tco2e_val, act_months, desc = calculate_time_weighted_emissions(up_charge, gwp_val, up_inst, scrap_str, inventory_year)
                    
                    # 判斷報廢年度是否為「前一年」或更早
                    move_to_scrap_archive = False
                    if is_scrap and scrap_str:
                        s_year = pd.to_datetime(scrap_str).year
                        if s_year < inventory_year:
                            move_to_scrap_archive = True
                            
                    if move_to_scrap_archive:
                        # 自動自在役台帳移除，移動到報廢清單
                        scrapped_item = curr_row.to_dict()
                        scrapped_item.update({
                            "具體裝設地點": up_loc,
                            "額定充填量 (kg/台)": up_charge,
                            "設備總充填量 (kg)": up_charge,
                            "設置日期": up_inst,
                            "報廢日期": scrap_str,
                            "當年度有效月數": 0,
                            "潛在排放總量 (tCO2e)": 0.0,
                            "設備狀態": "已報廢除役"
                        })
                        st.session_state.scrapped_inventory = pd.concat([
                            st.session_state.scrapped_inventory, pd.DataFrame([scrapped_item])
                        ], ignore_index=True)
                        st.session_state.refrigerant_inventory = st.session_state.refrigerant_inventory.drop(row_idx).reset_index(drop=True)
                        st.warning(f"📦 設備【{target_id}】報廢日期為 {scrap_str}（早於當前盤查年 {inventory_year}），已自動移至「📦 報廢/除役設備清冊」，當年度不計入排放！")
                    else:
                        # 仍留在在役清單中，但更新當年度實際月數加權碳排
                        st.session_state.refrigerant_inventory.at[row_idx, "具體裝設地點"] = up_loc
                        st.session_state.refrigerant_inventory.at[row_idx, "額定充填量 (kg/台)"] = up_charge
                        st.session_state.refrigerant_inventory.at[row_idx, "設備總充填量 (kg)"] = up_charge
                        st.session_state.refrigerant_inventory.at[row_idx, "設置日期"] = up_inst
                        st.session_state.refrigerant_inventory.at[row_idx, "報廢日期"] = scrap_str
                        st.session_state.refrigerant_inventory.at[row_idx, "設備狀態"] = "已報廢除役" if is_scrap else up_status
                        st.session_state.refrigerant_inventory.at[row_idx, "當年度有效月數"] = act_months
                        st.session_state.refrigerant_inventory.at[row_idx, "潛在排放總量 (tCO2e)"] = tco2e_val
                        st.success(f"✅ 成功更新設備【{target_id}】！{desc}，當年度加權排放量為：{tco2e_val} tCO2e")
                    st.rerun()

    # 模式 C: 刪除設備
    elif change_mode == "🗑️ 刪除誤登設備":
        df_active = st.session_state.refrigerant_inventory
        if df_active.empty:
            st.info("目前清單無任何設備可刪除。")
        else:
            del_id = st.selectbox("請選擇欲永久刪除的設備編號：", df_active["設備編號 (Asset ID)"].unique())
            del_row = df_active[df_active["設備編號 (Asset ID)"] == del_id].iloc[0]
            st.error(f"⚠️ 即將刪除設備：**{del_id}**（{del_row['具體裝設地點']} - {del_row['冷媒種類']} {del_row['額定充填量 (kg/台)']}kg）")
            if st.button("❌ 確認永久刪除此筆設備", type="primary"):
                st.session_state.refrigerant_inventory = df_active[df_active["設備編號 (Asset ID)"] != del_id].reset_index(drop=True)
                st.success(f"🗑️ 已成功刪除設備【{del_id}】！")
                st.rerun()

# ==================== Tab 4: 台帳總覽、編修與佐證歸檔 ====================
with tab_manage:
    st.subheader("📋 在役設備台帳總覽、線上編修與點選上傳佐證")
    df_current = st.session_state.refrigerant_inventory
    
    if df_current.empty:
        st.info("目前清單內尚無在役資料，請先透過【批次匯入】、【銘牌 AI 辨識】或【設備異動】新增設備。")
    else:
        with st.expander("📸 點選設備上傳銘牌佐證（自動更名為設備編號歸檔）", expanded=False):
            u_col1, u_col2, u_col3 = st.columns([2, 2, 1])
            with u_col1:
                target_asset_list = list(df_current["設備編號 (Asset ID)"].unique())
                target_asset_id = st.selectbox("請選擇目標設備編號：", target_asset_list, key="target_asset_select")
                target_info = df_current[df_current["設備編號 (Asset ID)"] == target_asset_id].iloc[0]
                st.caption(f"設備：{target_info['具體裝設地點']} | {target_info['廠牌']} | 現有佐證：`{target_info['原廠銘牌佐證']}`")
            with u_col2:
                single_doc_file = st.file_uploader("上傳銘牌照片 / PDF", type=["jpg", "jpeg", "png", "pdf"], key=f"upload_for_{target_asset_id}")
            with u_col3:
                st.write("")
                st.write("")
                if st.button("📤 確認歸檔綁定", type="primary"):
                    if single_doc_file is None:
                        st.warning("請先選取要上傳的檔案！")
                    else:
                        ext = single_doc_file.name.split(".")[-1]
                        saved_name = f"{target_asset_id}_銘牌.{ext}"
                        save_target_path = EVIDENCE_DIR / saved_name
                        with open(save_target_path, "wb") as f:
                            f.write(single_doc_file.read())
                        t_idx = st.session_state.refrigerant_inventory[st.session_state.refrigerant_inventory["設備編號 (Asset ID)"] == target_asset_id].index
                        st.session_state.refrigerant_inventory.loc[t_idx, "原廠銘牌佐證"] = saved_name
                        st.success(f"✅ 成功將銘牌歸檔為 `{saved_name}`！")
                        st.rerun()

        # 多選篩選
        f_col1, f_col2, f_col3 = st.columns(3)
        with f_col1:
            all_refs = sorted(list(df_current["冷媒種類"].dropna().unique()))
            selected_refs = st.multiselect("依冷媒種類篩選 (可多選)", options=all_refs, default=[], placeholder="預設顯示全部冷媒種類")
        with f_col2:
            all_sites = sorted(list(df_current["廠區/據點代碼"].dropna().unique()))
            selected_sites = st.multiselect("依廠區/據點篩選 (可多選)", options=all_sites, default=[], placeholder="預設顯示全部廠區據點")
        with f_col3:
            all_types = sorted(list(df_current["設備類別"].dropna().unique()))
            selected_types = st.multiselect("依設備類別篩選 (可多選)", options=all_types, default=[], placeholder="預設顯示全部設備類別")
            
        df_filtered = df_current.copy()
        if selected_refs:
            df_filtered = df_filtered[df_filtered["冷媒種類"].isin(selected_refs)]
        if selected_sites:
            df_filtered = df_filtered[df_filtered["廠區/據點代碼"].isin(selected_sites)]
        if selected_types:
            df_filtered = df_filtered[df_filtered["設備類別"].isin(selected_types)]
            
        # 統計指標看板 (反映月數加權之年度碳排)
        k1, k2, k3, k4 = st.columns(4)
        k1.metric("在役設備總數", f"{len(df_filtered)} 台")
        k2.metric("冷媒在役總存量", f"{df_filtered['設備總充填量 (kg)'].sum():,.2f} kg")
        k3.metric("主要冷媒類型", df_filtered["冷媒種類"].mode()[0] if not df_filtered.empty else "-")
        k4.metric(f"{inventory_year} 年度加權排放當量", f"{df_filtered['潛在排放總量 (tCO2e)'].sum():,.2f} tCO2e", help="已考量當年度新設/報廢之運轉月數加權")
        
        edited_df = st.data_editor(
            df_filtered,
            use_container_width=True,
            num_rows="dynamic",
            key="inventory_editor"
        )
        
        c_save, c_d1, c_d2 = st.columns([2, 1, 1])
        with c_save:
            if st.button("💾 儲存表格修改並重新核算", type="primary"):
                for idx, row in edited_df.iterrows():
                    asset_id = row["設備編號 (Asset ID)"]
                    ref_norm = clean_ref_name(row["冷媒種類"])
                    gwp_val = current_gwp_dict.get(ref_norm, 0)
                    try:
                        rate = float(row["額定充填量 (kg/台)"])
                    except:
                        rate = 0.0
                    try:
                        qty = int(row["數量 (台)"])
                    except:
                        qty = 1
                    tot = round(rate * qty, 3)
                    
                    # 重新計算時間加權
                    tco2e, act_m, _ = calculate_time_weighted_emissions(tot, gwp_val, str(row["設置日期"]), str(row["報廢日期"]), inventory_year)
                    
                    target_idx = st.session_state.refrigerant_inventory[st.session_state.refrigerant_inventory["設備編號 (Asset ID)"] == asset_id].index
                    if not target_idx.empty:
                        st.session_state.refrigerant_inventory.loc[target_idx, "冷媒種類"] = ref_norm
                        st.session_state.refrigerant_inventory.loc[target_idx, "額定充填量 (kg/台)"] = rate
                        st.session_state.refrigerant_inventory.loc[target_idx, "數量 (台)"] = qty
                        st.session_state.refrigerant_inventory.loc[target_idx, "設備總充填量 (kg)"] = tot
                        st.session_state.refrigerant_inventory.loc[target_idx, "對應 GWP 值"] = gwp_val
                        st.session_state.refrigerant_inventory.loc[target_idx, "設置日期"] = str(row["設置日期"])
                        st.session_state.refrigerant_inventory.loc[target_idx, "報廢日期"] = str(row["報廢日期"])
                        st.session_state.refrigerant_inventory.loc[target_idx, "當年度有效月數"] = act_m
                        st.session_state.refrigerant_inventory.loc[target_idx, "潛在排放總量 (tCO2e)"] = tco2e
                        st.session_state.refrigerant_inventory.loc[target_idx, "具體裝設地點"] = row["具體裝設地點"]
                        st.session_state.refrigerant_inventory.loc[target_idx, "廠牌"] = row["廠牌"]
                        st.session_state.refrigerant_inventory.loc[target_idx, "機型編號"] = row["機型編號"]
                        st.session_state.refrigerant_inventory.loc[target_idx, "設備狀態"] = row["設備狀態"]
                        st.session_state.refrigerant_inventory.loc[target_idx, "原廠銘牌佐證"] = row["原廠銘牌佐證"]
                        
                st.success("✅ 台帳資料已成功儲存並同步更新碳排計算！")
                st.rerun()

        with c_d1:
            csv_bytes = st.session_state.refrigerant_inventory.to_csv(index=False).encode('utf-8-sig')
            st.download_button("📥 匯出在役清冊 (CSV)", csv_bytes, "冷媒在役台帳.csv", "text/csv")
            
        with c_d2:
            excel_buf = io.BytesIO()
            with pd.ExcelWriter(excel_buf, engine='openpyxl') as writer:
                st.session_state.refrigerant_inventory.to_excel(writer, index=False, sheet_name="在役冷媒台帳")
            st.download_button("📥 匯出在役清冊 (Excel)", excel_buf.getvalue(), "冷媒在役台帳.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

# ==================== Tab 5: 報廢/除役設備清冊 ====================
with tab_scrap:
    st.subheader("📦 歷史報廢 / 除役設備封存清單")
    st.markdown(f"此處封存報廢日期早於盤查基準年（<{inventory_year} 年）之設備。這些設備已自當期組織邊界排除（碳排記為 0 $tCO_2e$），但保留完整審計軌跡以利查證！")
    
    df_scrapped = st.session_state.scrapped_inventory
    if df_scrapped.empty:
        st.info("目前無任何報廢封存設備。")
    else:
        sk1, sk2 = st.columns(2)
        sk1.metric("已報廢設備總數", f"{len(df_scrapped)} 台")
        sk2.metric("累積報廢冷媒量", f"{df_scrapped['設備總充填量 (kg)'].sum():,.2f} kg")
        
        st.dataframe(df_scrapped, use_container_width=True, hide_index=True)
        
        excel_scrap_buf = io.BytesIO()
        with pd.ExcelWriter(excel_scrap_buf, engine='openpyxl') as writer:
            df_scrapped.to_excel(writer, index=False, sheet_name="報廢設備清冊")
        st.download_button("📥 匯出報廢設備存證清冊 (Excel)", excel_scrap_buf.getvalue(), "報廢冷媒設備存證清冊.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
