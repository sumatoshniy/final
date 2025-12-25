from flask import Flask, render_template, request, redirect, flash, url_for, session, send_file
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from datetime import datetime, timedelta
import cx_Oracle
import io

app = Flask(__name__)
app.config['SECRET_KEY'] = 'secret123'

# Инициализация Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login_page'

# Конфигурация подключения к Oracle
ORACLE_CONFIG = {
    'user': 'S100058',
    'password': 'S100058',
    'dsn': '10.4.30.43:1521/test'
}


def get_oracle_connection():
    """Создает подключение к Oracle"""
    try:
        connection = cx_Oracle.connect(**ORACLE_CONFIG)
        return connection
    except cx_Oracle.Error as e:
        print(f"Ошибка подключения к Oracle: {e}")
        return None


# Модель пользователя для Flask-Login
class User(UserMixin):
    def __init__(self, id, email, kpo=None):
        self.id = id
        self.email = email
        self.kpo = kpo


# Загрузчик пользователя для Flask-Login
@login_manager.user_loader
def load_user(user_id):
    """Загружает пользователя из сессии"""
    user_email = session.get('user_email')
    user_kpo = session.get('user_kpo')
    if user_email:
        return User(int(user_id), user_email, user_kpo)
    return None


# Функция для проверки наличия PDF для договора
def has_pdf_for_contract(contract_num):
    """Проверяет, есть ли PDF файл для договора"""
    try:
        connection = get_oracle_connection()
        if not connection:
            return False

        cursor = connection.cursor()
        cursor.execute("""
            SELECT COUNT(*) 
            FROM CONTRACT_PDF 
            WHERE CONTRACT_NUM = :contract_num
        """, contract_num=contract_num)

        count = cursor.fetchone()[0]
        cursor.close()
        connection.close()

        return count > 0
    except cx_Oracle.Error as e:
        print(f"Ошибка проверки PDF: {e}")
        return False


# Функция проверки прав администратора (по email)
def check_admin():
    """Проверяет, является ли пользователь администратором"""
    if not current_user.is_authenticated:
        return False

    # Администратор только пользователь с email admin@bk.ru
    return current_user.email.lower() == 'admin@bk.ru'


# ГЛАВНАЯ СТРАНИЦА
@app.route("/")
def index():
    return render_template('index.html')


# POST обработчик для входа
@app.route("/login", methods=['POST'])
def login():
    mail = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()

    if not mail or not password:
        flash('Заполните все поля', 'danger')
        return redirect('/')

    try:
        connection = get_oracle_connection()
        if not connection:
            flash('Ошибка подключения к базе данных', 'danger')
            return redirect('/')

        cursor = connection.cursor()
        cursor.execute("""
            SELECT PERS_AUT_ID, MAIL, PASSWORD, KSOST, PERS_ROOM_ID 
            FROM PERS_ROOM_AUT 
            WHERE MAIL = :mail
        """, mail=mail)

        result = cursor.fetchone()

        if not result:
            cursor.close()
            connection.close()
            flash('Пользователь не зарегистрирован', 'danger')
            return redirect('/')

        user_id, user_mail, user_password, ksost, pers_room_id = result

        if ksost == 2:
            cursor.close()
            connection.close()
            flash('Пользователь заблокирован, обратитесь к менеджеру', 'warning')
            return redirect('/')

        if ksost == 1 and user_password == password:
            cursor.execute("""
                SELECT KPO FROM PERS_ROOM 
                WHERE PERS_ROOM_ID = :pers_room_id
            """, pers_room_id=pers_room_id)

            kpo_result = cursor.fetchone()
            kpo = kpo_result[0] if kpo_result else None

            cursor.close()
            connection.close()

            session['user_email'] = user_mail
            session['user_kpo'] = kpo

            user = User(user_id, user_mail, kpo)
            login_user(user)

            flash('Вы успешно вошли!', 'success')
            return redirect('/profile')

        cursor.close()
        connection.close()
        flash('ОШИБКА! Неверный пароль', 'danger')
        return redirect('/')

    except cx_Oracle.Error as e:
        print(f"Ошибка базы данных: {e}")
        flash('Ошибка базы данных', 'danger')
        return redirect('/')


# Функция для получения организации пользователя
def get_current_organization():
    """Получаем организацию текущего пользователя из Oracle"""
    if current_user.is_authenticated and current_user.kpo:
        try:
            connection = get_oracle_connection()
            if connection:
                cursor = connection.cursor()
                cursor.execute("""
                    SELECT NPO, INN, ADRES 
                    FROM KL_PRED 
                    WHERE KPO = :kpo
                """, kpo=current_user.kpo)

                result = cursor.fetchone()
                cursor.close()
                connection.close()

                if result:
                    npo, inn, adres = result
                    return {
                        'npo': npo,
                        'inn': inn,
                        'adres': adres
                    }
        except cx_Oracle.Error as e:
            print(f"Ошибка получения организации: {e}")
    return None


# Маршрут profile
@app.route("/profile")
@login_required
def profile():
    organization = get_current_organization()
    if not organization:
        flash('Организация не найдена', 'danger')
        return redirect('/')
    return render_template('profile.html', organization=organization)


# Маршрут contracts
@app.route("/contracts", methods=['GET'])
@login_required
def contracts():
    print(f"\n📋 ПОЛЬЗОВАТЕЛЬ: {current_user.email}, KPO: {current_user.kpo}")

    if not current_user.kpo:
        flash('Организация не найдена', 'danger')
        return redirect('/profile')

    try:
        connection = get_oracle_connection()
        if not connection:
            flash('Ошибка подключения к базе данных', 'danger')
            return redirect('/profile')

        cursor = connection.cursor()

        # Получаем параметры запроса
        start_date_str = request.args.get('start_date')
        end_date_str = request.args.get('end_date')
        show_all = request.args.get('show_all') == 'true'

        # Флаг, что пользователь явно запросил договора
        user_requested = start_date_str or end_date_str or show_all

        print(f"   📅 Параметры запроса: start_date={start_date_str}, end_date={end_date_str}, show_all={show_all}")
        print(f"   🎯 Пользователь явно запросил: {user_requested}")

        # Если пользователь не запросил договора явно, показываем пустой список
        if not user_requested:
            print(f"   ⚠️  Пользователь не запросил договора - показываем пустой список")

            # Устанавливаем даты для отображения (последний год)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=365)

            contracts_list = []
            filtered_count = 0

            # Получаем общее количество договоров для информации
            cursor.execute("""
                SELECT COUNT(*) 
                FROM REG_DOGOVOR 
                WHERE KPO = :kpo 
                AND SUBSTR(NUM_DOG, -1) NOT IN ('Т', 'И')
            """, kpo=current_user.kpo)
            total_contracts = cursor.fetchone()[0]
            print(f"   📊 Всего договоров для KPO={current_user.kpo}: {total_contracts}")

            cursor.close()
            connection.close()

            date_display = {
                'start_date': start_date.strftime('%d.%m.%Y'),
                'end_date': end_date.strftime('%d.%m.%Y'),
                'start_date_input': start_date.strftime('%Y-%m-%d'),
                'end_date_input': end_date.strftime('%Y-%m-%d'),
                'show_all': False
            }

            return render_template('contracts.html',
                                   contracts=contracts_list,
                                   dates=date_display,
                                   kpo=current_user.kpo,
                                   total_contracts=total_contracts,
                                   filtered_count=filtered_count,
                                   is_admin=check_admin())

        # SQL запрос - разный в зависимости от режима
        if show_all:
            sql_query = """
                SELECT 
                    rd.NUM_DOG,
                    rd.DATA_REG,
                    rd.DAT_BEG_DOG,
                    rd.DAT_END_DOG,
                    kd.NAIM_DOG,
                    ks.NAME
                FROM REG_DOGOVOR rd
                LEFT JOIN KL_DOGOVOR kd ON rd.KOD_VID_DOG = kd.KOD_VID_DOG
                LEFT JOIN KL_SORT_PROD ks ON rd.PREDM_DOG = ks.KOD_UKR_SORT
                WHERE rd.KPO = :kpo 
                AND SUBSTR(rd.NUM_DOG, -1) NOT IN ('Т', 'И')
                ORDER BY rd.DATA_REG DESC
            """
            params = {'kpo': current_user.kpo}
            print(f"   🔍 Режим: ПОКАЗАТЬ ВСЕ договора")

            cursor.execute("""
                SELECT MIN(DATA_REG), MAX(DATA_REG) 
                FROM REG_DOGOVOR 
                WHERE KPO = :kpo
            """, kpo=current_user.kpo)
            min_max_dates = cursor.fetchone()
            print(f"   📊 Min/Max даты для KPO={current_user.kpo}: {min_max_dates}")

            if min_max_dates and min_max_dates[0] and min_max_dates[1]:
                start_date = min_max_dates[0]
                end_date = min_max_dates[1]
            else:
                start_date = datetime.now() - timedelta(days=365)
                end_date = datetime.now()

        else:
            # Фильтрация по датам
            if start_date_str and end_date_str:
                try:
                    start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
                    end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
                except ValueError:
                    flash('Неверный формат даты', 'danger')
                    start_date = datetime.now() - timedelta(days=365)
                    end_date = datetime.now()
            else:
                # Если даты не указаны, показываем за последний год
                end_date = datetime.now()
                start_date = end_date - timedelta(days=365)

            sql_query = """
                SELECT 
                    rd.NUM_DOG,
                    rd.DATA_REG,
                    rd.DAT_BEG_DOG,
                    rd.DAT_END_DOG,
                    kd.NAIM_DOG,
                    ks.NAME
                FROM REG_DOGOVOR rd
                LEFT JOIN KL_DOGOVOR kd ON rd.KOD_VID_DOG = kd.KOD_VID_DOG
                LEFT JOIN KL_SORT_PROD ks ON rd.PREDM_DOG = ks.KOD_UKR_SORT
                WHERE rd.KPO = :kpo 
                AND rd.DATA_REG BETWEEN :start_date AND :end_date
                AND SUBSTR(rd.NUM_DOG, -1) NOT IN ('Т', 'И')
                ORDER BY rd.DATA_REG DESC
            """
            params = {'kpo': current_user.kpo, 'start_date': start_date, 'end_date': end_date}
            print(f"   🔍 Режим: ФИЛЬТРАЦИЯ по датам {start_date} - {end_date}")

        print(f"   🗃️  SQL запрос с параметрами: kpo={current_user.kpo}")

        cursor.execute(sql_query, params)
        contracts_data = cursor.fetchall()
        print(f"   ✅ Найдено договоров: {len(contracts_data)}")

        # Проверим общее количество договоров для этого KPO
        cursor.execute("""
            SELECT COUNT(*) 
            FROM REG_DOGOVOR 
            WHERE KPO = :kpo 
            AND SUBSTR(NUM_DOG, -1) NOT IN ('Т', 'И')
        """, kpo=current_user.kpo)
        total_contracts = cursor.fetchone()[0]
        print(f"   📊 Всего договоров для KPO={current_user.kpo}: {total_contracts}")

        cursor.close()
        connection.close()

        # Обработка данных
        contracts_list = []
        for contract in contracts_data:
            num_dog, data_reg, dat_beg_dog, dat_end_dog, naim_dog, name = contract

            data_reg_str = data_reg.strftime('%d.%m.%Y') if data_reg else ''
            dat_beg_str = dat_beg_dog.strftime('%d.%m.%Y') if dat_beg_dog else ''
            dat_end_str = dat_end_dog.strftime('%d.%m.%Y') if dat_end_dog else ''
            period_str = f"{dat_beg_str} – {dat_end_str}" if dat_beg_str and dat_end_str else ''

            has_pdf = has_pdf_for_contract(num_dog)

            contracts_list.append({
                'num_dog': num_dog,
                'data_reg': data_reg_str,
                'period': period_str,
                'vid_dog': naim_dog or '',
                'predmet': name or '',
                'has_pdf': has_pdf
            })

        # Даты для отображения
        if show_all:
            date_display = {
                'start_date': start_date.strftime('%d.%m.%Y') if hasattr(start_date, 'strftime') else '—',
                'end_date': end_date.strftime('%d.%m.%Y') if hasattr(end_date, 'strftime') else '—',
                'start_date_input': start_date.strftime('%Y-%m-%d') if hasattr(start_date, 'strftime') else '',
                'end_date_input': end_date.strftime('%Y-%m-%d') if hasattr(end_date, 'strftime') else '',
                'show_all': True
            }
        else:
            date_display = {
                'start_date': start_date.strftime('%d.%m.%Y'),
                'end_date': end_date.strftime('%d.%m.%Y'),
                'start_date_input': start_date.strftime('%Y-%m-%d'),
                'end_date_input': end_date.strftime('%Y-%m-%d'),
                'show_all': False
            }

        print(f"   📋 Передано в шаблон договоров: {len(contracts_list)}")

        return render_template('contracts.html',
                               contracts=contracts_list,
                               dates=date_display,
                               kpo=current_user.kpo,
                               total_contracts=total_contracts,
                               filtered_count=len(contracts_list),
                               is_admin=check_admin())

    except cx_Oracle.Error as e:
        print(f"❌ Ошибка получения договоров: {e}")
        flash('Ошибка получения данных', 'danger')

    print(f"   ⚠️  Возвращаем пустой список договоров")

    # Возвращаем пустой список если ошибка
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365)

    return render_template('contracts.html', contracts=[], dates={
        'start_date': start_date.strftime('%d.%m.%Y'),
        'end_date': end_date.strftime('%d.%m.%Y'),
        'start_date_input': start_date.strftime('%Y-%m-%d'),
        'end_date_input': end_date.strftime('%Y-%m-%d'),
        'show_all': False
    }, kpo=current_user.kpo, total_contracts=0, filtered_count=0, is_admin=check_admin())


# Маршрут для загрузки PDF - ТОЛЬКО ДЛЯ АДМИНИСТРАТОРА (admin@bk.ru)
@app.route("/upload_pdf", methods=['GET', 'POST'])
@login_required
def upload_pdf():
    # Проверяем права администратора
    if not check_admin():
        flash('У вас нет прав для загрузки PDF файлов', 'danger')
        return redirect(url_for('contracts'))

    if request.method == 'POST':
        contract_num = request.form.get('contract_num', '').strip()
        pdf_file = request.files.get('pdf_file')

        if not contract_num:
            flash('Введите номер договора', 'danger')
            return redirect(url_for('upload_pdf'))

        if not pdf_file or not pdf_file.filename:
            flash('Выберите PDF файл', 'danger')
            return redirect(url_for('upload_pdf'))

        if not pdf_file.filename.lower().endswith('.pdf'):
            flash('Файл должен быть в формате PDF', 'danger')
            return redirect(url_for('upload_pdf'))

        try:
            pdf_content = pdf_file.read()

            connection = get_oracle_connection()
            if not connection:
                flash('Ошибка подключения к базе данных', 'danger')
                return redirect(url_for('upload_pdf'))

            cursor = connection.cursor()

            cursor.execute("""
                SELECT COUNT(*) 
                FROM CONTRACT_PDF 
                WHERE CONTRACT_NUM = :contract_num
            """, contract_num=contract_num)

            exists = cursor.fetchone()[0] > 0

            if exists:
                cursor.execute("""
                    UPDATE CONTRACT_PDF 
                    SET PDF_CONTENT = :pdf_content,
                        FILE_NAME = :file_name,
                        UPLOAD_DATE = SYSDATE
                    WHERE CONTRACT_NUM = :contract_num
                """, {
                    'pdf_content': pdf_content,
                    'file_name': pdf_file.filename,
                    'contract_num': contract_num
                })
                message = 'PDF файл обновлен'
            else:
                cursor.execute("""
                    INSERT INTO CONTRACT_PDF (CONTRACT_NUM, PDF_CONTENT, FILE_NAME)
                    VALUES (:contract_num, :pdf_content, :file_name)
                """, {
                    'contract_num': contract_num,
                    'pdf_content': pdf_content,
                    'file_name': pdf_file.filename
                })
                message = 'PDF файл успешно загружен'

            connection.commit()
            cursor.close()
            connection.close()

            flash(message, 'success')
            return redirect(url_for('contracts'))

        except cx_Oracle.Error as e:
            print(f"Ошибка загрузки PDF: {e}")
            flash(f'Ошибка при загрузке файла: {e}', 'danger')
            return redirect(url_for('upload_pdf'))
        except Exception as e:
            print(f"Ошибка обработки файла: {e}")
            flash(f'Ошибка при обработке файла: {e}', 'danger')
            return redirect(url_for('upload_pdf'))

    return render_template('upload_pdf.html')


# Маршрут для управления PDF - ТОЛЬКО ДЛЯ АДМИНИСТРАТОРА
@app.route("/manage_pdf")
@login_required
def manage_pdf():
    # Проверяем права администратора
    if not check_admin():
        flash('У вас нет прав для управления PDF файлами', 'danger')
        return redirect(url_for('contracts'))

    try:
        connection = get_oracle_connection()
        if not connection:
            flash('Ошибка подключения к базе данных', 'danger')
            return redirect(url_for('contracts'))

        cursor = connection.cursor()

        # Получаем все PDF файлы
        cursor.execute("""
            SELECT CONTRACT_NUM, FILE_NAME, UPLOAD_DATE 
            FROM CONTRACT_PDF 
            ORDER BY UPLOAD_DATE DESC
        """)

        pdf_files = cursor.fetchall()

        # Форматируем даты
        formatted_pdfs = []
        for pdf in pdf_files:
            contract_num, file_name, upload_date = pdf
            upload_date_str = upload_date.strftime('%d.%m.%Y %H:%M:%S') if upload_date else ''
            formatted_pdfs.append({
                'contract_num': contract_num,
                'file_name': file_name,
                'upload_date': upload_date_str
            })

        cursor.close()
        connection.close()

        return render_template('manage_pdf.html', pdf_files=formatted_pdfs)

    except cx_Oracle.Error as e:
        print(f"Ошибка получения списка PDF: {e}")
        flash('Ошибка при получении списка PDF файлов', 'danger')
        return redirect(url_for('upload_pdf'))


# Маршрут для удаления PDF - ТОЛЬКО ДЛЯ АДМИНИСТРАТОРА
@app.route("/delete_pdf/<contract_num>")
@login_required
def delete_pdf(contract_num):
    # Проверяем права администратора
    if not check_admin():
        flash('У вас нет прав для удаления PDF файлов', 'danger')
        return redirect(url_for('contracts'))

    try:
        connection = get_oracle_connection()
        if not connection:
            flash('Ошибка подключения к базе данных', 'danger')
            return redirect(url_for('contracts'))

        cursor = connection.cursor()

        # Проверяем, существует ли файл
        cursor.execute("""
            SELECT COUNT(*) 
            FROM CONTRACT_PDF 
            WHERE CONTRACT_NUM = :contract_num
        """, contract_num=contract_num)

        exists = cursor.fetchone()[0] > 0

        if exists:
            cursor.execute("""
                DELETE FROM CONTRACT_PDF 
                WHERE CONTRACT_NUM = :contract_num
            """, contract_num=contract_num)

            connection.commit()
            cursor.close()
            connection.close()

            flash('PDF файл успешно удален', 'success')
        else:
            cursor.close()
            connection.close()
            flash('PDF файл не найден', 'warning')

        return redirect(url_for('contracts'))

    except cx_Oracle.Error as e:
        print(f"Ошибка удаления PDF: {e}")
        flash(f'Ошибка при удалении файла: {e}', 'danger')
        return redirect(url_for('contracts'))


# Маршрут для просмотра PDF - ДЛЯ ВСЕХ ПОЛЬЗОВАТЕЛЕЙ
@app.route("/view_pdf/<contract_num>")
@login_required
def view_pdf(contract_num):
    try:
        connection = get_oracle_connection()
        if not connection:
            flash('Ошибка подключения к базе данных', 'danger')
            return redirect(url_for('contracts'))

        cursor = connection.cursor()

        # Получаем BLOB и имя файла
        cursor.execute("""
            SELECT PDF_CONTENT, FILE_NAME 
            FROM CONTRACT_PDF 
            WHERE CONTRACT_NUM = :contract_num
        """, contract_num=contract_num)

        result = cursor.fetchone()

        if not result:
            # Пробуем найти по частичному совпадению
            cursor.execute("""
                SELECT PDF_CONTENT, FILE_NAME 
                FROM CONTRACT_PDF 
                WHERE CONTRACT_NUM LIKE '%' || :contract_num || '%'
            """, contract_num=contract_num)

            result = cursor.fetchone()

            if not result:
                cursor.close()
                connection.close()
                flash('PDF файл не найден', 'danger')
                return redirect(url_for('contracts'))

        # Правильно читаем BLOB
        pdf_blob, file_name = result

        # Читаем BLOB полностью перед закрытием курсора
        pdf_data = pdf_blob.read()

        cursor.close()
        connection.close()

        # Создаем объект BytesIO из прочитанных данных
        pdf_io = io.BytesIO(pdf_data)

        return send_file(
            pdf_io,
            mimetype='application/pdf',
            as_attachment=False,
            download_name=file_name
        )

    except cx_Oracle.Error as e:
        print(f"Ошибка получения PDF: {e}")
        flash('Ошибка при получении файла', 'danger')
        return redirect(url_for('contracts'))
    except Exception as e:
        print(f"Ошибка обработки PDF: {e}")
        flash('Ошибка при обработке файла', 'danger')
        return redirect(url_for('contracts'))


# Выход
@app.route("/logout")
@login_required
def logout():
    session.clear()
    logout_user()
    flash('Вы вышли из системы', 'info')
    return redirect('/')


# Страница "О нас"
@app.route("/about")
def about():
    return render_template('about.html')


if __name__ == '__main__':
    app.run(debug=True, port=5000)


