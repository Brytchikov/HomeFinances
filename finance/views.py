import secrets
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.hashers import make_password, check_password
from django.core.mail import send_mail
from django.db import IntegrityError
from django.db import transaction as db_transaction

from django.db.models.functions import Coalesce
from django.shortcuts import render, redirect
from django.views.decorators.http import require_POST, require_http_methods
from django.db.models import Sum, DecimalField, Value, Exists, OuterRef

from .models import (Account, Role, AccountRole, UserProfile, Client,
                     Wallet, Transaction, Expense, ExpenseCategory,
                     Income, IncomeCategory, Goal, Budget)

CODE_LIFETIME = 600      # 10 минут
RESEND_DELAY = 60        # повторная отправка не чаще раза в минуту
MAX_ATTEMPTS = 5
MIN_TABLE_ROWS = 3       # сколько строк таблицы показываем минимум
MAX_NAME_LENGTH = 100    # длина названия кошелька / категории (проверьте по models.py)

# под DecimalField(max_digits=14, decimal_places=2)
MAX_AMOUNT = Decimal('999999999999.99')

WALLET_NAMES = {'cash': 'Наличные', 'card': 'Банковская карта'}

# что пользователь может ввести вручную -> как это хранится в базе
WALLET_ALIASES = {
    'cash': 'Наличные', 'card': 'Банковская карта',
    'наличные': 'Наличные', 'наличка': 'Наличные', 'нал': 'Наличные',
    'банковская карта': 'Банковская карта', 'карта': 'Банковская карта',
    'карточка': 'Банковская карта', 'банк': 'Банковская карта',
}

EXPENSE_CATEGORY_NAMES = {
    'food': 'Продукты', 'cafes': 'Кафе и рестораны', 'transport': 'Транспорт',
    'fuel': 'Топливо', 'housing': 'Жильё и аренда',
    'utilities': 'Коммунальные услуги', 'shopping': 'Одежда и обувь',
    'health': 'Здоровье и лекарства', 'beauty': 'Красота и уход',
    'entertainment': 'Развлечения', 'subscriptions': 'Подписки и сервисы',
    'communication': 'Связь и интернет', 'education': 'Образование',
    'travel': 'Путешествия', 'children': 'Дети',
    'pets': 'Домашние животные', 'household': 'Товары для дома',
    'gifts': 'Подарки и благотворительность', 'insurance': 'Страхование',
    'other': 'Другое',
}

INCOME_CATEGORY_NAMES = {
    'salary': 'Зарплата', 'bonus': 'Премия',
    'freelance': 'Подработка и фриланс', 'business': 'Доход от бизнеса',
    'investments': 'Инвестиции и проценты', 'rent': 'Сдача в аренду',
    'pension': 'Пенсия и пособия', 'gifts': 'Подарки',
    'sale': 'Продажа вещей', 'other': 'Другое',
}

CONFIRM_SESSION_KEYS = ('confirm_code', 'confirm_time', 'confirm_attempts',
                        'confirm_email', 'pending_registration')

DATE_FORMATS = ('%d.%m.%Y', '%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d')


# ---------- Вспомогательные функции ----------

def client_required(view):
    """Пускает только вошедших клиентов и передаёт client в представление."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        account_id = request.session.get('account_id')
        if not account_id:
            return redirect('enter_site')
        client = Client.objects.filter(account_id=account_id).first()
        if client is None:
            return redirect('enter_site')
        return view(request, client, *args, **kwargs)
    return wrapper


def get_or_create_first(model, **kwargs):
    """Как get_or_create, но не падает, если в БД уже есть дубликаты."""
    obj = model.objects.filter(**kwargs).order_by('pk').first()
    if obj is None:
        obj = model.objects.create(**kwargs)
    return obj


def parse_amount(raw):
    """Возвращает (Decimal, None) при успехе или (None, текст ошибки)."""
    raw = (raw or '').strip().replace(',', '.')
    if not raw:
        return None, 'Введите сумму'

    try:
        amount = Decimal(raw)
    except (InvalidOperation, TypeError):
        return None, 'Введите корректную сумму'

    if not amount.is_finite():           # NaN, Infinity
        return None, 'Введите корректную сумму'

    try:
        amount = amount.quantize(Decimal('0.01'))
    except InvalidOperation:             # слишком большое число
        return None, 'Слишком большая сумма'

    if amount <= 0:
        return None, 'Сумма должна быть больше нуля'
    if amount > MAX_AMOUNT:
        return None, 'Слишком большая сумма'

    return amount, None


def parse_date(raw):
    """
    Ручной ввод даты (ДД.ММ.ГГГГ).
    Возвращает (date, None) или (None, текст ошибки).
    Пустая дата = сегодня (так работает и форма доходов без поля даты).
    """
    raw = (raw or '').strip()
    if not raw:
        return date.today(), None

    for fmt in DATE_FORMATS:
        try:
            value = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        if value > date.today():
            return None, 'Дата не может быть в будущем'
        return value, None

    return None, 'Введите дату в формате ДД.ММ.ГГГГ'


def clean_text(raw):
    """Убирает лишние пробелы и переносы строк."""
    return ' '.join((raw or '').split())


def normalize_name(text):
    """'  кафе   и РЕСТОРАНЫ ' -> 'Кафе и рестораны' (чтобы не плодить дубликаты)."""
    text = clean_text(text)
    return text[:1].upper() + text[1:].lower()


def resolve_wallet(raw, empty_error):
    """Ручной ввод вида кошелька -> (название, None) или (None, ошибка)."""
    text = clean_text(raw)
    if not text:
        return None, empty_error
    if len(text) > MAX_NAME_LENGTH:
        return None, f'Название кошелька не длиннее {MAX_NAME_LENGTH} символов'

    alias = WALLET_ALIASES.get(text.lower())
    return (alias or normalize_name(text)), None


def resolve_category(raw, category_names, empty_error):
    """Ручной ввод категории -> (название, None) или (None, ошибка)."""
    text = clean_text(raw)
    if not text:
        return None, empty_error
    if len(text) > MAX_NAME_LENGTH:
        return None, f'Название категории не длиннее {MAX_NAME_LENGTH} символов'

    if text in category_names:                       # ключ из старой формы ('salary')
        return category_names[text], None

    known = {name.lower(): name for name in category_names.values()}
    return known.get(text.lower(), normalize_name(text)), None


def parse_fio(fio):
    """'Иванов Иван Иванович' -> (фамилия, имя, отчество)"""
    parts = fio.split()
    last_name = parts[0] if len(parts) > 0 else ''
    first_name = parts[1] if len(parts) > 1 else ''
    middle_name = ' '.join(parts[2:]) if len(parts) > 2 else ''
    return last_name, first_name, middle_name


# ---------- Регистрация и подтверждение ----------

def send_confirmation_code(request, email):
    code = f"{secrets.randbelow(10**6):06d}"

    request.session['confirm_code'] = code
    request.session['confirm_email'] = email
    request.session['confirm_time'] = time.time()
    request.session['confirm_attempts'] = 0

    send_mail(
        subject='Код подтверждения HomeFinances',
        message=(
            f'Ваш код подтверждения: {code}\n\n'
            'Код действует 10 минут.\n'
            'Если вы не регистрировались в HomeFinances, просто проигнорируйте это письмо.'
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )


def registration_view(request):
    if request.method == 'POST':
        fio = request.POST.get('fio', '').strip()
        email = request.POST.get('email', '').strip().lower()
        dob = request.POST.get('dob', '').strip()
        username = request.POST.get('username', '').strip()
        password = request.POST.get('password', '')

        if not fio or not email or not username or not password:
            messages.error(request, 'Заполните ФИО, email, логин и пароль')
            return render(request, 'register_page.html')

        last_name, first_name, middle_name = parse_fio(fio)
        if not first_name:
            messages.error(request, 'Введите фамилию и имя (и отчество, если есть)')
            return render(request, 'register_page.html')

        birth_date = None
        if dob:
            try:
                birth_date = datetime.strptime(dob, '%d.%m.%Y').date()
            except ValueError:
                messages.error(request, 'Дата рождения должна быть в формате ДД.ММ.ГГГГ')
                return render(request, 'register_page.html')
            if birth_date > date.today():
                messages.error(request, 'Дата рождения не может быть в будущем')
                return render(request, 'register_page.html')

        if Account.objects.filter(email=email).exists():
            messages.error(request, 'Этот email уже зарегистрирован')
            return render(request, 'register_page.html')

        if Account.objects.filter(username=username).exists():
            messages.error(request, 'Этот логин уже занят')
            return render(request, 'register_page.html')

        # Пароль хранится только в виде хеша.
        # Дата кладётся строкой: объект date не сериализуется в JSON.
        request.session['pending_registration'] = {
            'email': email,
            'username': username,
            'password_hash': make_password(password),
            'first_name': first_name,
            'last_name': last_name,
            'middle_name': middle_name,
            'birth_date': birth_date.isoformat() if birth_date else None,
        }

        try:
            send_confirmation_code(request, email)
        except Exception:
            messages.error(request, 'Не удалось отправить письмо. Попробуйте позже.')
            return render(request, 'register_page.html')

        return redirect('confirmation')

    return render(request, 'register_page.html')


def create_account_from_session(request):
    data = request.session['pending_registration']
    birth_date = (date.fromisoformat(data['birth_date'])
                  if data['birth_date'] else None)

    with db_transaction.atomic():
        account = Account.objects.create(
            username=data['username'],
            email=data['email'],
            password_hash=data['password_hash'],
        )
        role, _ = Role.objects.get_or_create(role_name='client')
        AccountRole.objects.create(account=account, role=role)
        UserProfile.objects.create(
            account=account,
            first_name=data['first_name'],
            last_name=data['last_name'],
            middle_name=data['middle_name'],
            birth_date=birth_date,
        )
        Client.objects.create(account=account)

    return account


def confirmation(request):
    if 'confirm_code' not in request.session or 'pending_registration' not in request.session:
        return redirect('register_page')

    if request.method == 'POST':
        entered = request.POST.get('code', '').strip()
        saved = request.session['confirm_code']
        created = request.session.get('confirm_time', 0)
        attempts = request.session.get('confirm_attempts', 0)

        if attempts >= MAX_ATTEMPTS:
            messages.error(request, 'Слишком много попыток. Запросите новый код.')
        elif time.time() - created > CODE_LIFETIME:
            messages.error(request, 'Код устарел. Запросите новый.')
        elif secrets.compare_digest(entered.encode(), saved.encode()):
            try:
                create_account_from_session(request)
            except IntegrityError:
                for key in CONFIRM_SESSION_KEYS:
                    request.session.pop(key, None)
                messages.error(request, 'Такой логин или email уже зарегистрирован.')
                return redirect('register_page')

            for key in CONFIRM_SESSION_KEYS:
                request.session.pop(key, None)
            messages.success(request, 'Email подтверждён. Войдите в аккаунт.')
            return redirect('enter_site')
        else:
            request.session['confirm_attempts'] = attempts + 1
            messages.error(request, 'Неверный код')

    return render(request, 'confirmation.html', {
        'email': request.session.get('confirm_email'),
    })


@require_POST
def resend_code(request):
    email = request.session.get('confirm_email')
    if not email or 'pending_registration' not in request.session:
        return redirect('register_page')

    if time.time() - request.session.get('confirm_time', 0) < RESEND_DELAY:
        messages.error(request, 'Подождите минуту перед повторной отправкой.')
        return redirect('confirmation')

    try:
        send_confirmation_code(request, email)
        messages.success(request, 'Новый код отправлен на почту.')
    except Exception:
        messages.error(request, 'Не удалось отправить письмо. Попробуйте позже.')

    return redirect('confirmation')


# ---------- Вход, выход, главная ----------

def enter_site(request):
    if request.session.get('account_id'):
        return redirect('main_page')

    errors = {}
    login_value = ''

    if request.method == 'POST':
        login_value = request.POST.get('login', '').strip()
        password = request.POST.get('password', '')

        if not login_value:
            errors['login'] = 'Введите логин'
        elif len(login_value) > 150:
            errors['login'] = 'Логин слишком длинный'

        if not password:
            errors['password'] = 'Введите пароль'
        elif len(password) > 128:
            errors['password'] = 'Пароль слишком длинный'

        if not errors:
            account = Account.objects.filter(username=login_value).first()

            if account and check_password(password, account.password_hash):
                request.session.cycle_key()
                request.session['account_id'] = account.id
                return redirect('main_page')

            errors['common'] = 'Неверный логин или пароль'

    return render(request, 'enter_site.html', {
        'errors': errors,
        'login_value': login_value,
    })


@require_http_methods(['GET'])
@client_required
def main_page(request, client):
    return render(request, 'main_page.html')


@require_POST
def logout_view(request):
    request.session.flush()
    return redirect('enter_site')


# ---------- Расходы и доходы ----------

def _normalize_newlines(raw):
    return (raw or '').replace('\r\n', '\n').replace('\r', '\n')


def _read_table_rows(request):
    """
    Собирает строки таблицы из POST. Поля date/wallet/category/amount
    приходят списками (по одному значению на строку таблицы).
    В полях wallet/category/amount переносы строк сохраняются:
    каждая строка внутри ячейки — отдельная запись.
    Работает и со старой формой из одной строки.
    """
    dates = request.POST.getlist('date')
    wallets = request.POST.getlist('wallet')
    categories = request.POST.getlist('category')
    amounts = request.POST.getlist('amount')

    count = max(len(dates), len(wallets), len(categories), len(amounts))

    def pick(values, i, keep_newlines=False):
        if i >= len(values):
            return ''
        value = values[i]
        return _normalize_newlines(value) if keep_newlines else value.strip()

    return [{
        'date': pick(dates, i),
        'wallet': pick(wallets, i, True),
        'category': pick(categories, i, True),
        'amount': pick(amounts, i, True),
    } for i in range(count)]


def _empty_row():
    return {'date': '', 'wallet': '', 'category': '', 'amount': ''}


def _split_lines(raw):
    """Текст ячейки -> список строк (хвостовые пустые строки отбрасываются)."""
    lines = [clean_text(line) for line in _normalize_newlines(raw).split('\n')]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _row_error(number, text, record=None):
    where = f'Строка {number}'
    if record is not None:
        where += f', запись {record}'
    return f'{where}: {text[:1].lower()}{text[1:]}'


def _parse_row(number, row, category_names, wallet_error, category_error):
    """
    Разбирает одну строку таблицы на записи.
    Возвращает (список записей, текст ошибки).
    Запись = (кошелёк, категория, сумма, дата).

    Каждая строка внутри ячейки — отдельная запись. Если в поле кошелька
    или категории указано одно значение, а сумм несколько, оно
    применяется ко всем записям.
    """
    wallets = _split_lines(row['wallet'])
    categories = _split_lines(row['category'])
    amounts = _split_lines(row['amount'])

    total = max(len(wallets), len(categories), len(amounts))

    if total == 0:
        if not row['date']:
            return [], None                    # совсем пустая строка таблицы
        total = 1                              # заполнена только дата -> ошибка ниже

    row_date, date_error = parse_date(row['date'])
    if date_error:
        return [], _row_error(number, date_error)

    def prepare(lines):
        """(значения, общее_ли_значение)"""
        shared = len(lines) == 1 and total > 1
        return lines + [''] * (total - len(lines)), shared

    wallets, wallet_shared = prepare(wallets)
    categories, category_shared = prepare(categories)
    amounts, amount_shared = prepare(amounts)

    records = []
    for i in range(total):
        # пустая строка-разделитель внутри ячеек пропускается
        own_values = []
        if not wallet_shared:
            own_values.append(wallets[i])
        if not category_shared:
            own_values.append(categories[i])
        if not amount_shared:
            own_values.append(amounts[i])
        if total > 1 and not any(own_values):
            continue

        wallet_raw = wallets[0] if wallet_shared else wallets[i]
        category_raw = categories[0] if category_shared else categories[i]
        amount_raw = amounts[0] if amount_shared else amounts[i]

        record = i + 1 if total > 1 else None

        wallet_name, err = resolve_wallet(wallet_raw, wallet_error)
        if err:
            return [], _row_error(number, err, record)

        category_name, err = resolve_category(
            category_raw, category_names, category_error)
        if err:
            return [], _row_error(number, err, record)

        amount, err = parse_amount(amount_raw)
        if err:
            return [], _row_error(number, err, record)

        records.append((wallet_name, category_name, amount, row_date))

    return records, None


def add_transaction(request, client, *, template, url_name, category_names,
                    category_model, record_model, wallet_error,
                    category_error, success_text):
    """Общая логика добавления расходов или доходов (несколько строк таблицы)."""
    rows = []
    added = False        # True, если пользователь нажал «Продолжить таблицу»

    if request.method == 'POST' and request.POST.get('action') == 'add_row':
        # Ничего не сохраняем: возвращаем введённое и добавляем пустую строку.
        rows = _read_table_rows(request)
        rows.append(_empty_row())
        added = True

    elif request.method == 'POST':
        rows = _read_table_rows(request)

        parsed = []
        error = None

        for number, row in enumerate(rows, start=1):
            records, error = _parse_row(
                number, row, category_names, wallet_error, category_error)
            if error:
                break
            parsed.extend(records)

        if error:
            messages.error(request, error)
        elif not parsed:
            messages.error(request, 'Заполните хотя бы одну строку таблицы')
        else:
            wallets_cache = {}
            categories_cache = {}

            with db_transaction.atomic():
                for wallet_name, category_name, amount, row_date in parsed:
                    if wallet_name not in wallets_cache:
                        wallets_cache[wallet_name] = get_or_create_first(
                            Wallet, client=client, name=wallet_name)
                    if category_name not in categories_cache:
                        categories_cache[category_name] = get_or_create_first(
                            category_model, name=category_name)

                    tr = Transaction.objects.create(
                        amount=amount,
                        date=row_date,
                        wallet=wallets_cache[wallet_name],
                    )
                    record_model.objects.create(
                        transaction=tr, category=categories_cache[category_name])

            if len(parsed) == 1:
                messages.success(request, success_text)
            else:
                messages.success(request, f'{success_text} (записей: {len(parsed)})')
            return redirect(url_name)

    # дополняем таблицу пустыми строками до минимума
    while len(rows) < MIN_TABLE_ROWS:
        rows.append(_empty_row())

    return render(request, template, {
        'wallets': list(WALLET_NAMES.items()),
        'categories': list(category_names.items()),
        'rows': rows,
        'added': added,
    })


@require_http_methods(['GET', 'POST'])
@client_required
def expenses(request, client):
    return add_transaction(
        request, client,
        template='expenses.html',
        url_name='expenses',
        category_names=EXPENSE_CATEGORY_NAMES,
        category_model=ExpenseCategory,
        record_model=Expense,
        wallet_error='Введите вид кошелька',
        category_error='Введите категорию расходов',
        success_text='Расход сохранён',
    )


@require_http_methods(['GET', 'POST'])
@client_required
def income(request, client):
    return add_transaction(
        request, client,
        template='incomes.html',
        url_name='income',
        category_names=INCOME_CATEGORY_NAMES,
        category_model=IncomeCategory,
        record_model=Income,
        wallet_error='Введите вид кошелька',
        category_error='Введите категорию доходов',
        success_text='Доход сохранён',
    )


# ---------- Цели ----------

MIN_GOAL_ROWS = 3            # сколько строк таблицы ввода показываем минимум
MAX_GOAL_NAME = 200
MAX_GOAL_DESCRIPTION = 2000


def _empty_goal_row():
    return {'name': '', 'description': '', 'amount': ''}


def _read_goal_rows(request):
    """Строки таблицы ввода: поля приходят списками, по одному значению на строку."""
    keys = ('name', 'description', 'amount')
    columns = {key: request.POST.getlist(key) for key in keys}
    count = max(len(values) for values in columns.values())

    return [{
        key: (_normalize_newlines(columns[key][i]).strip()
              if i < len(columns[key]) else '')
        for key in keys
    } for i in range(count)]


def _parse_goal_row(number, row):
    """Возвращает (цель или None для пустой строки, текст ошибки)."""
    if not any(row.values()):
        return None, None

    name = clean_text(row['name'])
    if not name:
        return None, _row_error(number, 'Введите название цели')
    if len(name) > MAX_GOAL_NAME:
        return None, _row_error(
            number, f'Название не длиннее {MAX_GOAL_NAME} символов')

    # в описании переносы строк сохраняем, убираем только пробелы по краям строк
    description = '\n'.join(line.strip() for line in row['description'].split('\n')).strip()
    if len(description) > MAX_GOAL_DESCRIPTION:
        return None, _row_error(
            number, f'Описание не длиннее {MAX_GOAL_DESCRIPTION} символов')

    amount, err = parse_amount(clean_text(row['amount']))
    if err:
        return None, _row_error(number, err)

    return {'name': name, 'description': description, 'amount': amount}, None


@require_http_methods(['GET', 'POST'])
@client_required
def goals(request, client):
    """GET — таблица ввода и список целей, POST — добавление целей."""
    rows = []
    added = False        # True, если пользователь нажал «Продолжить таблицу»

    if request.method == 'POST' and request.POST.get('action') == 'add_row':
        # Ничего не сохраняем: возвращаем введённое и добавляем пустую строку.
        rows = _read_goal_rows(request)
        rows.append(_empty_goal_row())
        added = True

    elif request.method == 'POST':
        rows = _read_goal_rows(request)

        parsed = []
        error = None
        for number, row in enumerate(rows, start=1):
            item, error = _parse_goal_row(number, row)
            if error:
                break
            if item:
                parsed.append(item)

        if error:
            messages.error(request, error)
        elif not parsed:
            messages.error(request, 'Заполните хотя бы одну строку таблицы')
        else:
            with db_transaction.atomic():
                for item in parsed:
                    Goal.objects.create(
                        client=client,
                        name=item['name'],
                        description=item['description'],
                        target_count=item['amount'],
                    )

            if len(parsed) == 1:
                messages.success(request, 'Цель добавлена')
            else:
                messages.success(request, f'Цели добавлены (записей: {len(parsed)})')
            return redirect('goals')

    # дополняем таблицу ввода пустыми строками до минимума
    while len(rows) < MIN_GOAL_ROWS:
        rows.append(_empty_goal_row())

    return render(request, 'goals.html', {
        'rows': rows,
        'goals': client.goals.order_by('-id'),
        'added': added,
    })


# ---------- Отчёт ----------

def _total(client, relation):
    """Сумма транзакций клиента, у которых есть запись в Income или Expense."""
    return Transaction.objects.filter(
        wallet__client=client, **{f'{relation}__isnull': False}
    ).aggregate(
        total=Coalesce(Sum('amount'), Value(Decimal('0.00')),
                       output_field=DecimalField(max_digits=16, decimal_places=2))
    )['total']


# ---------- Отчёт ----------

MIN_REPORT_ROWS = 3      # сколько строк таблицы показываем минимум

KIND_LABELS = {'income': 'Доход', 'expense': 'Расход'}


@require_http_methods(['GET'])
@client_required
def report(request, client):
    transactions = (Transaction.objects
                    .filter(wallet__client=client)
                    .select_related('wallet')
                    .prefetch_related('incomes__category', 'expenses__category')
                    .order_by('-date', '-id'))

    operations = []
    for tr in transactions:
        incomes = list(tr.incomes.all())
        expenses_ = list(tr.expenses.all())
        if incomes:
            kind, category = 'income', incomes[0].category.name
        elif expenses_:
            kind, category = 'expense', expenses_[0].category.name
        else:
            continue
        operations.append({
            'kind': kind,
            'title': f'{KIND_LABELS[kind]}: {category}',
            'wallet': tr.wallet.name,
            'date': tr.date,
            'amount': tr.amount,
        })

    # дополняем таблицу пустыми строками до минимума
    while len(operations) < MIN_REPORT_ROWS:
        operations.append({'kind': '', 'title': '', 'wallet': '',
                           'date': None, 'amount': ''})

    return render(request, 'report.html', {'operations': operations})

## ---------- Бюджет ----------

MIN_BUDGET_ROWS = 3      # сколько строк таблицы ввода показываем минимум
MAX_BUDGET_NAME = 200    # как max_length у Budget.name


def parse_budget_date(raw, title):
    """
    Дата для бюджета. В отличие от parse_date здесь дата обязательна,
    а будущие даты разрешены (бюджет обычно задают наперёд).
    """
    raw = (raw or '').strip()
    if not raw:
        return None, f'Введите дату {title}'
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date(), None
        except ValueError:
            continue
    return None, f'Введите дату {title} в формате ДД.ММ.ГГГГ'


def _empty_budget_row():
    return {'name': '', 'wallet': '', 'limit': '', 'start': '', 'end': ''}


def _read_budget_rows(request):
    """Строки таблицы ввода: поля приходят списками, по одному значению на строку."""
    keys = ('name', 'wallet', 'limit', 'start', 'end')
    columns = {key: request.POST.getlist(key) for key in keys}
    count = max(len(values) for values in columns.values())

    return [{
        key: (columns[key][i].strip() if i < len(columns[key]) else '')
        for key in keys
    } for i in range(count)]


def _parse_budget_row(number, row):
    """Возвращает (бюджет или None для пустой строки, текст ошибки)."""
    if not any(row.values()):
        return None, None

    name = clean_text(row['name'])
    if not name:
        return None, _row_error(number, 'Введите название бюджета')
    if len(name) > MAX_BUDGET_NAME:
        return None, _row_error(
            number, f'Название не длиннее {MAX_BUDGET_NAME} символов')

    wallet_name, err = resolve_wallet(row['wallet'], 'Введите вид кошелька')
    if err:
        return None, _row_error(number, err)

    limit, err = parse_amount(clean_text(row['limit']))
    if err:
        return None, _row_error(number, err)

    start, err = parse_budget_date(row['start'], 'начала')
    if err:
        return None, _row_error(number, err)

    end, err = parse_budget_date(row['end'], 'конца')
    if err:
        return None, _row_error(number, err)

    if end < start:
        return None, _row_error(number, 'Дата конца раньше даты начала')

    return {'name': name, 'wallet': wallet_name, 'limit': limit,
            'start': start, 'end': end}, None


def _spent(budget):
    """Расходы по кошельку бюджета за его период."""
    return (Transaction.objects
            .filter(wallet=budget.wallet,
                    date__range=(budget.start_date, budget.end_date))
            .filter(Exists(Expense.objects.filter(transaction=OuterRef('pk'))))
            .aggregate(total=Coalesce(
                Sum('amount'), Value(Decimal('0.00')),
                output_field=DecimalField(max_digits=16, decimal_places=2)))
            ['total'])


@require_http_methods(['GET', 'POST'])
@client_required
def budget(request, client):
    rows = []
    added = False        # True, если пользователь нажал «Продолжить таблицу»

    if request.method == 'POST' and request.POST.get('action') == 'add_row':
        # Ничего не сохраняем: возвращаем введённое и добавляем пустую строку.
        rows = _read_budget_rows(request)
        rows.append(_empty_budget_row())
        added = True

    elif request.method == 'POST':
        rows = _read_budget_rows(request)

        parsed = []
        error = None
        for number, row in enumerate(rows, start=1):
            item, error = _parse_budget_row(number, row)
            if error:
                break
            if item:
                parsed.append(item)

        if error:
            messages.error(request, error)
        elif not parsed:
            messages.error(request, 'Заполните хотя бы одну строку таблицы')
        else:
            wallets_cache = {}
            with db_transaction.atomic():
                for item in parsed:
                    if item['wallet'] not in wallets_cache:
                        wallets_cache[item['wallet']] = get_or_create_first(
                            Wallet, client=client, name=item['wallet'])
                    Budget.objects.create(
                        name=item['name'],
                        limit_amount=item['limit'],
                        start_date=item['start'],
                        end_date=item['end'],
                        wallet=wallets_cache[item['wallet']],
                    )

            if len(parsed) == 1:
                messages.success(request, 'Бюджет сохранён')
            else:
                messages.success(request, f'Бюджеты сохранены (записей: {len(parsed)})')
            return redirect('budget')

    # дополняем таблицу ввода пустыми строками до минимума
    while len(rows) < MIN_BUDGET_ROWS:
        rows.append(_empty_budget_row())

    saved = []
    for b in (Budget.objects
              .filter(wallet__client=client)
              .select_related('wallet')
              .order_by('-end_date', '-id')):
        spent = _spent(b)
        left = b.limit_amount - spent
        saved.append({
            'name': b.name,
            'wallet': b.wallet.name,
            'limit': b.limit_amount,
            'start': b.start_date,
            'end': b.end_date,
            'spent': spent,
            'left': left,
            'over': left < 0,
        })

    return render(request, 'budget.html', {
        'rows': rows,
        'budgets': saved,
        'added': added,
    })
# ---------- Профиль ----------

MAX_FIO = 200
MAX_USERNAME = 150
MAX_PASSWORD = 128
MIN_PASSWORD = 6


def _profile_values(account, profile_obj):
    """Текущие данные пользователя для показа в таблице."""
    fio = ''
    birth = ''
    if profile_obj is not None:
        fio = ' '.join(part for part in (profile_obj.last_name,
                                         profile_obj.first_name,
                                         profile_obj.middle_name) if part)
        if profile_obj.birth_date:
            birth = profile_obj.birth_date.strftime('%d.%m.%Y')
    return {'fio': fio, 'birth': birth, 'username': account.username}


def _parse_birth_date(raw):
    """Дата рождения необязательна. Возвращает (date или None, ошибка)."""
    raw = (raw or '').strip()
    if not raw:
        return None, None
    for fmt in DATE_FORMATS:
        try:
            value = datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
        if value > date.today():
            return None, 'Дата рождения не может быть в будущем'
        return value, None
    return None, 'Введите дату рождения в формате ДД.ММ.ГГГГ'


@require_http_methods(['GET', 'POST'])
@client_required
def profile(request, client):
    account = client.account
    profile_obj = UserProfile.objects.filter(account=account).first()
    values = _profile_values(account, profile_obj)

    if request.method == 'POST':
        values = {
            'fio': clean_text(request.POST.get('fio', '')),
            'birth': request.POST.get('birth', '').strip(),
            'username': clean_text(request.POST.get('username', '')),
        }
        old_password = request.POST.get('old_password', '')
        new_password = request.POST.get('new_password', '')

        error = None
        birth_date = None
        last_name = first_name = middle_name = ''

        # ФИО
        if not values['fio']:
            error = 'Введите ФИО'
        elif len(values['fio']) > MAX_FIO:
            error = f'ФИО не длиннее {MAX_FIO} символов'
        else:
            last_name, first_name, middle_name = parse_fio(values['fio'])
            if not first_name:
                error = 'Введите фамилию и имя (и отчество, если есть)'

        # дата рождения
        if not error:
            birth_date, error = _parse_birth_date(values['birth'])

        # логин
        if not error:
            if not values['username']:
                error = 'Введите логин'
            elif len(values['username']) > MAX_USERNAME:
                error = f'Логин не длиннее {MAX_USERNAME} символов'
            elif (Account.objects.filter(username=values['username'])
                  .exclude(pk=account.pk).exists()):
                error = 'Этот логин уже занят'

        # пароль: меняем, только если заполнено хотя бы одно поле
        change_password = bool(old_password or new_password)
        if not error and change_password:
            if not old_password:
                error = 'Введите текущий пароль'
            elif not new_password:
                error = 'Введите новый пароль'
            elif not check_password(old_password, account.password_hash):
                error = 'Текущий пароль указан неверно'
            elif len(new_password) < MIN_PASSWORD:
                error = f'Новый пароль не короче {MIN_PASSWORD} символов'
            elif len(new_password) > MAX_PASSWORD:
                error = 'Новый пароль слишком длинный'

        if error:
            messages.error(request, error)
        else:
            try:
                with db_transaction.atomic():
                    account.username = values['username']
                    if change_password:
                        account.password_hash = make_password(new_password)
                    account.save()

                    if profile_obj is None:
                        profile_obj = UserProfile(account=account)
                    profile_obj.last_name = last_name
                    profile_obj.first_name = first_name
                    profile_obj.middle_name = middle_name
                    profile_obj.birth_date = birth_date
                    profile_obj.save()
            except IntegrityError:
                messages.error(request, 'Этот логин уже занят')
            else:
                messages.success(
                    request,
                    'Данные и пароль сохранены' if change_password
                    else 'Данные сохранены')
                return redirect('profile')

    return render(request, 'profile.html', {
        'values': values,
        'email': account.email,
    })