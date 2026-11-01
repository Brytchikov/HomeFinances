from django.db import models


class Account(models.Model):
    username = models.CharField(max_length=150, unique=True)
    password_hash = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    roles = models.ManyToManyField('Role', through='AccountRole', related_name='accounts')

    def __str__(self):
        return self.username


class Role(models.Model):
    role_name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.role_name


class AccountRole(models.Model):
    account = models.ForeignKey(Account, on_delete=models.CASCADE)
    role = models.ForeignKey(Role, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('account', 'role')


class UserProfile(models.Model):
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100)
    middle_name = models.CharField(max_length=100, blank=True)
    birth_date = models.DateField(null=True, blank=True)
    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name='profile')


class Client(models.Model):
    account = models.OneToOneField(Account, on_delete=models.CASCADE, related_name='client')


class Goal(models.Model):
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    target_count = models.DecimalField(max_digits=14, decimal_places=2)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='goals')


class Wallet(models.Model):
    name = models.CharField(max_length=200)
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='wallets')


class Budget(models.Model):
    name = models.CharField(max_length=200)
    limit_amount = models.DecimalField(max_digits=14, decimal_places=2)
    start_date = models.DateField()
    end_date = models.DateField()
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='budgets')


class Tag(models.Model):
    name = models.CharField(max_length=100, unique=True)


class Transaction(models.Model):
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    date = models.DateField()
    wallet = models.ForeignKey(Wallet, on_delete=models.CASCADE, related_name='transactions')
    tags = models.ManyToManyField(Tag, through='TransactionTag', related_name='transactions')


class TransactionTag(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE)
    tag = models.ForeignKey(Tag, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('transaction', 'tag')



class ExpenseCategory(models.Model):
    name = models.CharField(max_length=100)


class IncomeCategory(models.Model):
    name = models.CharField(max_length=100)


class Expense(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='expenses')
    category = models.ForeignKey(ExpenseCategory, on_delete=models.PROTECT)


class Income(models.Model):
    transaction = models.ForeignKey(Transaction, on_delete=models.CASCADE, related_name='incomes')
    category = models.ForeignKey(IncomeCategory, on_delete=models.PROTECT)